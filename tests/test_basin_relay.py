"""実ネットワークを使わず、転送列とoffset確定順の安全条件を検証する。"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infra.cloudflare import basin_relay as relay


def configuration():
    """実接続値を持たない合成の専用group設定を返す。"""
    return {
        "source_id": "synthetic-mes",
        "capture_epoch": "demo-v1",
        "group_id": "lakehouse-basin-synthetic",
        "endpoint": "https://synthetic.ingest.cloudflare.com",
        "kafka": {"bootstrap.servers": "localhost:9092"},
        "topics": {
            "synthetic.orders": {
                "alias": "orders",
                "table": "orders",
                "columns": ["id", "quantity"],
                "key_columns": ["id"],
            }
        },
    }


def policy():
    """射影テストで使う、主キーと数量だけの送信契約を作る。"""
    return relay.Policy(
        "synthetic-mes",
        "demo-v1",
        "lakehouse-basin-synthetic",
        "https://synthetic.ingest.cloudflare.com",
        {
            "synthetic.orders": relay.TopicPolicy(
                "orders", "orders", ("id", "quantity"), ("id",)
            )
        },
        {"bootstrap.servers": "localhost:9092"},
    )


class Message:
    """Kafka SDKと同じ読み取り口を持つ合成イベント。通信は行わない。"""

    def __init__(
        self,
        *,
        operation="c",
        offset=3,
        partition=0,
        body="default",
        key=None,
        topic="synthetic.orders",
    ):
        """更新・削除・tombstone・不正値を独立に指定する。"""
        self._topic = topic
        self._partition = partition
        self._offset = offset
        self._key = json.dumps({"id": 7} if key is None else key).encode()
        self._value = (
            json.dumps(
                {
                    "op": operation,
                    "source": {
                        "db": "synthetic-private",
                        "host": "hidden.invalid",
                    },
                    "after": {
                        "id": 7,
                        "quantity": 12,
                        "password": "synthetic",
                    },
                    "before": {"id": 7, "quantity": 9},
                }
            ).encode()
            if body == "default"
            else body
        )

    def topic(self):
        """オンプレの実topic名に相当する合成名を返す。"""
        return self._topic

    def partition(self):
        """確定するpartitionを返す。"""
        return self._partition

    def offset(self):
        """読み取ったoffsetを返す。"""
        return self._offset

    def key(self):
        """合成主キーのJSONバイト列を返す。"""
        return self._key

    def value(self):
        """合成Debezium envelopeまたはtombstoneを返す。"""
        return self._value

    def error(self):
        """正常メッセージとして扱う。"""
        return None


class Sender:
    """受け取ったHTTP payloadをメモリ上にだけ保存する。"""

    def __init__(self, events, fail=False):
        """呼び出し順を外部リストで観測できるようにする。"""
        self.events = events
        self.fail = fail
        self.payloads = []

    def send(self, payload):
        """送信成功・応答不明の両方を合成的に作る。"""
        self.events.append("http")
        self.payloads.append(json.loads(payload))
        if self.fail:
            raise relay.RelayError("BASIN_SEND_UNCONFIRMED")


class ProjectionTests(unittest.TestCase):
    """更新・削除・再送・情報境界を、同じ変換規則で検証する。"""

    def test_only_selected_columns_and_logical_names_leave_on_prem(self):
        event = relay.project_event(Message(), policy())
        serialized = json.dumps(event)
        self.assertEqual(
            json.loads(event["after_json"]), {"id": 7, "quantity": 12}
        )
        self.assertNotIn("password", serialized)
        self.assertNotIn("hidden.invalid", serialized)
        self.assertNotIn("synthetic-private", serialized)
        self.assertNotIn("synthetic.orders", serialized)
        self.assertEqual(event["source_topic"], "orders")

    def test_create_update_and_snapshot_keep_their_operation(self):
        for operation in ("c", "u", "r"):
            with self.subTest(operation=operation):
                event = relay.project_event(
                    Message(operation=operation), policy()
                )
                self.assertEqual(event["operation"], operation)
                self.assertEqual(json.loads(event["after_json"])["id"], 7)

    def test_delete_and_tombstone_remain_different_events(self):
        deleted = relay.project_event(
            Message(operation="d", body=b'{"op":"d","before":{"id":7}}'),
            policy(),
        )
        tombstone = relay.project_event(Message(body=None), policy())
        self.assertEqual(deleted["operation"], "d")
        self.assertEqual(tombstone["operation"], "tombstone")
        self.assertEqual(tombstone["key_json"], '{"id": 7}')
        self.assertEqual(tombstone["after_json"], "null")

    def test_wrapped_connect_payload_is_supported(self):
        message = Message(
            body=b'{"schema":{},"payload":'
            b'{"op":"r","after":{"id":7,"quantity":5}}}'
        )
        event = relay.project_event(message, policy())
        self.assertEqual(json.loads(event["after_json"])["quantity"], 5)

    def test_replay_identity_is_stable_and_epoch_separates_new_capture(self):
        first = relay.project_event(Message(), policy())
        replay = relay.project_event(Message(), policy())
        restarted = relay.project_event(
            Message(), replace(policy(), capture_epoch="demo-v2")
        )
        self.assertEqual(first["event_id"], replay["event_id"])
        self.assertNotEqual(first["event_id"], restarted["event_id"])

    def test_invalid_keys_values_and_offsets_are_never_acknowledged(self):
        cases = [
            Message(key={"different": 7}),
            Message(offset=-1),
            Message(operation="invalid"),
            Message(body=b'{"op":"c","after":null}'),
            Message(body=b'{"op":"c","op":"d"}'),
            Message(body=b'{"op":"c","after":{"id":7,"quantity":NaN}}'),
        ]
        for message in cases:
            with self.subTest(message=message):
                with self.assertRaises(relay.RelayError):
                    relay.project_event(message, policy())


class DeliveryTests(unittest.TestCase):
    """HTTP受付より前に位置が進まないことを、故障注入で確認する。"""

    def test_ack_is_after_http_and_uses_next_offset_per_partition(self):
        order = []
        sender = Sender(order)
        commits = []

        def commit(positions):
            order.append("commit")
            commits.append(positions)

        count = relay.deliver(
            [
                Message(offset=3),
                Message(offset=8, partition=1),
                Message(offset=9, partition=1),
            ],
            policy(),
            sender,
            commit,
        )
        self.assertEqual(order, ["http", "commit"])
        self.assertEqual(
            commits,
            [{("synthetic.orders", 0): 4, ("synthetic.orders", 1): 10}],
        )
        self.assertEqual(count, 3)

    def test_failed_or_unknown_http_response_does_not_commit(self):
        order = []
        with self.assertRaises(relay.RelayError):
            relay.deliver(
                [Message()],
                policy(),
                Sender(order, fail=True),
                lambda _positions: order.append("commit"),
            )
        self.assertEqual(order, ["http"])

    def test_bad_second_record_prevents_the_entire_batch_from_sending(self):
        order = []
        with self.assertRaises(relay.RelayError):
            relay.deliver(
                [Message(), Message(operation="unsupported")],
                policy(),
                Sender(order),
                lambda _positions: order.append("commit"),
            )
        self.assertEqual(order, [])

    def test_uncertain_commit_can_replay_the_same_event_id(self):
        order = []
        sender = Sender(order)

        def failed_commit(_positions):
            raise relay.RelayError("KAFKA_COMMIT_UNCONFIRMED")

        with self.assertRaises(relay.RelayError):
            relay.deliver([Message()], policy(), sender, failed_commit)
        relay.deliver([Message()], policy(), sender, lambda _positions: None)
        self.assertEqual(
            sender.payloads[0][0]["event_id"],
            sender.payloads[1][0]["event_id"],
        )

    def test_oversized_payload_is_not_sent_or_committed(self):
        message = Message(
            body=json.dumps(
                {
                    "op": "c",
                    "after": {
                        "id": 7,
                        "quantity": "x" * relay.MAX_BATCH_BYTES,
                    },
                }
            ).encode()
        )
        order = []
        with self.assertRaises(relay.RelayError):
            relay.deliver(
                [message],
                policy(),
                Sender(order),
                lambda _positions: order.append("commit"),
            )
        self.assertEqual(order, [])


class ConfigurationTests(unittest.TestCase):
    """private設定、既定plan、固定送信先の境界を検査する。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "relay.json"

    def write(self, value):
        """テスト専用privateファイルだけを作る。実接続情報は使わない。"""
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_unknown_kafka_override_and_sensitive_columns_are_rejected(self):
        for change in ("auto_commit", "password", "missing_key", "group"):
            data = configuration()
            if change == "auto_commit":
                data["kafka"]["enable.auto.commit"] = "true"
            elif change == "password":
                data["topics"]["synthetic.orders"]["columns"].append(
                    "password"
                )
            elif change == "missing_key":
                data["topics"]["synthetic.orders"]["columns"] = ["quantity"]
            else:
                data["group_id"] = "existing-cdc-group"
            self.write(data)
            with self.subTest(change=change):
                with self.assertRaises(relay.RelayError):
                    relay.load_policy(self.path)

    def test_world_readable_and_symlink_settings_are_rejected(self):
        self.write(configuration())
        self.path.chmod(0o644)
        with self.assertRaises(relay.RelayError):
            relay.load_policy(self.path)
        self.path.chmod(0o600)
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        with self.assertRaises(relay.RelayError):
            relay.load_policy(link)

    def test_endpoint_rejects_credentials_query_other_hosts_and_http(self):
        for endpoint in (
            "http://synthetic.ingest.cloudflare.com",
            "https://example.invalid",
            "https://synthetic.ingest.cloudflare.com.evil.invalid",
            "https://token@synthetic.ingest.cloudflare.com",
            "https://synthetic.ingest.cloudflare.com?token=synthetic",
            "https://synthetic.ingest.cloudflare.com/other",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(relay.RelayError):
                    relay.validate_endpoint(endpoint)

    def test_plan_does_not_construct_a_sender_or_join_kafka(self):
        self.write(configuration())
        with (
            patch.object(sys, "argv", ["relay", "--config", str(self.path)]),
            patch.object(relay, "run") as run,
            patch.object(relay, "BasinHTTP") as sender,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(relay.main(), 0)
        run.assert_not_called()
        sender.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["network_used"])

    def test_http_failure_is_sanitized_and_redirects_are_disabled(self):
        sender = relay.BasinHTTP(
            "https://synthetic.ingest.cloudflare.com", "synthetic-only-token"
        )
        sender.opener = SimpleNamespace(
            open=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                urllib.error.URLError("private-response.invalid")
            )
        )
        with self.assertRaises(relay.RelayError) as raised:
            sender.send(b"[]")
        self.assertNotIn("private-response.invalid", str(raised.exception))
        self.assertIsNone(relay.NoRedirect().redirect_request())

    def test_http_success_preserves_bearer_header_and_json(
        self,
    ):
        requests = []
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 202

        def open_request(request, timeout):
            requests.append((request, timeout))
            return response

        sender = relay.BasinHTTP(
            "https://synthetic.ingest.cloudflare.com", "synthetic-only-token"
        )
        sender.opener = SimpleNamespace(open=open_request)
        sender.send(b'[{"event":"synthetic"}]')
        # 認証schemeと合成値を分け、具体的なBearer値を公開例に残さない。
        scheme, token = (
            requests[0][0].get_header("Authorization").split(" ", 1)
        )
        self.assertEqual(scheme, "Bearer")
        self.assertEqual(token, "synthetic-only-token")
        self.assertEqual(requests[0][0].data, b'[{"event":"synthetic"}]')

    def test_kafka_runtime_uses_manual_commit_and_always_closes(self):
        events = []
        options = {}

        class Consumer:
            """SDK境界を合成し、auto commitを有効にできないことを観測する。"""

            def __init__(self, settings):
                options.update(settings)

            def subscribe(self, topics):
                self.topics = topics

            def consume(self, num_messages, timeout):
                return [Message()]

            def commit(self, *, offsets, asynchronous):
                events.append("commit")
                self.offsets = offsets
                self.asynchronous = asynchronous
                return [SimpleNamespace(error=None)]

            def close(self):
                events.append("close")

        sdk = SimpleNamespace(
            Consumer=Consumer,
            TopicPartition=lambda *args: args,
        )
        with patch.dict(sys.modules, {"confluent_kafka": sdk}):
            count = relay.run(policy(), Sender(events), 1, 1, 1)
        self.assertEqual(count, 1)
        self.assertEqual(events, ["http", "commit", "close"])
        self.assertFalse(options["enable.auto.commit"])
        self.assertFalse(options["enable.auto.offset.store"])
        self.assertFalse(options["allow.auto.create.topics"])


if __name__ == "__main__":
    unittest.main()
