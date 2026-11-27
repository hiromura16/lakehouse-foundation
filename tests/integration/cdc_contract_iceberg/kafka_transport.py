"""閉じた disposable broker 以外に接続先を持たない試験用 transport。"""

import os
import re
import time
from contextlib import contextmanager

from proof import TOPIC, demand, fixture_record

BROKER = "contract-broker:9092"


class KafkaTransport:
    """一つの私有 broker に固定4イベントだけを配送する。"""

    def __init__(self):
        run_id = os.environ.get("CONTRACT_FIXTURE_ID", "")
        demand(
            re.fullmatch(r"cdc-contract-[0-9a-f]{24}", run_id) is not None
            and os.environ.get("CONTRACT_FIXTURE_HOSTED") == "true",
            "HOSTED_TRANSPORT_ONLY",
        )
        self.group = run_id

    def seed(self):
        """固定トピック一パーティションへの永続配送を同期的に確認する。"""
        from confluent_kafka import Producer

        producer = Producer(
            {
                "bootstrap.servers": BROKER,
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": 25000,
            }
        )
        metadata = producer.list_topics(timeout=20)
        topic = metadata.topics.get(TOPIC)
        demand(
            topic is not None
            and topic.error is None
            and set(topic.partitions) == {0},
            "BROKER_TOPIC_IDENTITY",
        )
        observed = []

        def delivered(error, message):
            """配送確定座標だけを保持し、例外本文やイベントは残さない。"""
            observed.append(
                None
                if error
                else (message.topic(), message.partition(), message.offset())
            )

        for index in range(4):
            record = fixture_record(index)
            payload = record["value"]
            producer.produce(
                TOPIC,
                partition=0,
                key=record["key"].encode("utf-8"),
                value=None if payload is None else payload.encode("utf-8"),
                on_delivery=delivered,
            )
        demand(
            producer.flush(30) == 0
            and observed == [(TOPIC, 0, index) for index in range(4)],
            "PRODUCER_DURABILITY",
        )

    @contextmanager
    def open(self):
        """auto commit/store を無効にし、再開は実 group offset に従う。"""
        from confluent_kafka import Consumer

        client = Consumer(
            {
                "bootstrap.servers": BROKER,
                "group.id": self.group,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
            }
        )
        try:
            client.subscribe([TOPIC])
            yield KafkaSession(client)
        finally:
            client.close()


class KafkaSession:
    """本来の codec と broker が返した同期 ACK を検証する。"""

    def __init__(self, client):
        self.client = client

    def receive(self, offset):
        """poll で得た実配送を、期待座標とワイヤ形に突き合わせる。"""
        from consumer import message_record

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            message = self.client.poll(0.5)
            if message is None:
                continue
            demand(message.error() is None, "BROKER_DELIVERY_ERROR")
            received = message_record(message)
            expected = fixture_record(offset)
            expected["ingested_at"] = received["ingested_at"]
            demand(received == expected, "BROKER_DELIVERY_MISMATCH")
            return received
        demand(False, "BROKER_DELIVERY_TIMEOUT")

    @staticmethod
    def check(result, expected=None):
        """返された topic/partition/offset とエラーを厳密に確認する。"""
        demand(
            isinstance(result, list)
            and len(result) == 1
            and result[0].topic == TOPIC
            and result[0].partition == 0
            and result[0].error is None
            and result[0].offset >= 0
            and (expected is None or result[0].offset == expected),
            "BROKER_POSITION_NOT_ATTESTED",
        )
        return result[0].offset

    def ack(self, offset):
        """期待 next offset への同期 commit を broker で確認する。"""
        from confluent_kafka import TopicPartition

        self.check(
            self.client.commit(
                offsets=[TopicPartition(TOPIC, 0, offset)], asynchronous=False
            ),
            offset,
        )

    def committed(self):
        """実 group offset を再照会する（メモリ位置ではない）。"""
        from confluent_kafka import TopicPartition

        return self.check(
            self.client.committed([TopicPartition(TOPIC, 0)], timeout=10)
        )
