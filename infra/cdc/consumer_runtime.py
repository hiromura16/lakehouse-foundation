"""Coordinate main-thread CDC processing with a read-only HTTP status cache.

Kafka, SQLite and Iceberg are accessed only by the consumer main thread.
Rebuild is an explicit queued maintenance operation. Failed raw validation or
derived materialization never advances the active checkpoint or Kafka ack.
"""

from cdc_replay import RebuildError, rebuild
from runtime_control import observed_at
from state import State, assignment_offset


class Runtime:
    """Own broker/state transitions while HTTP only reads cached metadata."""

    def __init__(
        self,
        admin,
        state,
        values,
        mapping,
        topics,
        control,
        broker,
        partition_factory,
        process_batch,
        materialize,
        *,
        lost_state=False,
    ):
        self.admin = admin
        self.state = state
        self.values = values
        self.mapping = mapping
        self.topics = topics
        self.control = control
        self.broker = broker
        self.partition_factory = partition_factory
        self.process_batch = process_batch
        self.materialize = materialize
        self.fixed = values.get("CDC_SOURCE_MODE") == "fixed-demo"
        self.maintenance = self.fixed and (
            lost_state
            or bool(state.get("rebuild_pending"))
            or bool(state.get("materialization_pending"))
            or state.get("blocked") == "true"
        )
        self.partitions = []
        self.error_code = "CDC_REBUILD_REQUIRED" if self.maintenance else None
        self.appended = 0
        self.observe()

    def blocked(self):
        """Retain query cancellation blockers across process restarts."""
        return self.state.get("blocked") == "true" or bool(
            self.admin.STATE.get("blocked")
        )

    def assign(self, client, partitions):
        """Check retained broker history before selecting a resume position."""
        bounds = {}
        for partition in partitions:
            if partition.topic not in self.topics:
                raise ValueError("Unexpected CDC partition assignment.")
            low, high = client.get_watermark_offsets(partition, timeout=10)
            bounds[(partition.topic, partition.partition)] = low
            try:
                partition.offset = (
                    low
                    if self.maintenance
                    else assignment_offset(
                        self.state.offset(
                            partition.topic, partition.partition
                        ),
                        low,
                        high,
                    )
                )
            except RuntimeError:
                if not self.fixed:
                    raise
                self.maintenance = True
                self.error_code = "CDC_REBUILD_REQUIRED"
        if self.maintenance:
            for partition in partitions:
                partition.offset = bounds[
                    (partition.topic, partition.partition)
                ]
        self.partitions = list(partitions)
        client.assign(partitions)
        if self.maintenance and partitions:
            client.pause(partitions)
        self.observe()

    def partition_status(self):
        """Observe checkpoints and broker bounds without private names."""
        result = []
        for partition in self.partitions:
            target = self.topics.get(partition.topic)
            if target is None:
                continue
            completed = self.state.offset(partition.topic, partition.partition)
            low = high = lag = None
            position = "unavailable"
            try:
                low, high = self.broker.get_watermark_offsets(
                    partition, timeout=3
                )
                position = (
                    "retention_gap"
                    if completed + 1 < low
                    else (
                        "broker_reset" if completed + 1 > high else "observed"
                    )
                )
                if position == "observed":
                    lag = high - completed - 1
            except Exception:
                low = high = None
            result.append(
                {
                    "table": target,
                    "partition": partition.partition,
                    "completed_offset": completed,
                    "broker_low": low,
                    "broker_high": high,
                    "next_offset": completed + 1,
                    "lag": lag,
                    "state": position,
                }
            )
        return result

    def observe(self, *, busy=False):
        """Refresh the cache on this thread, never from the HTTP handler."""
        blocked = self.blocked()
        pending = self.fixed and bool(
            self.state.get("rebuild_pending")
            or self.state.get("materialization_pending")
        )
        rebuilding = self.control.snapshot()["rebuild"]
        pending = pending or bool(
            rebuilding and rebuilding["status"] == "running"
        )
        initialized = bool(self.state.get("initialized"))
        generation = bool(self.state.completed_offsets())
        # An empty readable file is not an observed materialized generation.
        partitions = self.partition_status()
        observed = (
            initialized
            and generation
            and bool(partitions)
            and all(item["state"] == "observed" for item in partitions)
        )
        error = "CDC_QUERY_REVIEW_REQUIRED" if blocked else self.error_code
        if error is None and not observed:
            error = "CDC_STARTING"
        self.control.update(
            ready=observed and not (blocked or self.maintenance or pending),
            blocked=blocked,
            busy=busy,
            error_code=error,
            current_rows=self.state.counts(self.mapping),
            partitions=partitions,
            raw_events_appended_since_start=self.appended,
        )

    def resume_plan(self, candidate):
        """Validate candidate offsets against currently assigned Kafka data."""
        assigned = {(item.topic, item.partition) for item in self.partitions}
        plan = []
        for (
            topic,
            partition,
        ), completed in candidate.completed_offsets().items():
            if (topic, partition) not in assigned:
                raise RebuildError("CDC_KAFKA_ASSIGNMENT_REQUIRED")
            item = self.partition_factory(topic, partition)
            low, high = self.broker.get_watermark_offsets(item, timeout=10)
            item.offset = assignment_offset(completed, low, high)
            plan.append(item)
        return plan

    def reopen_active_state(self):
        """Reopen the active generation after close/publication errors."""
        path, identity = self.state.path, self.state.identity
        try:
            self.state.connection.close()
        except Exception:
            pass
        self.state = State(path, identity)

    def handle_rebuild(self):
        """Pause delivery, rebuild local state, then acknowledge Kafka."""
        request_id = self.control.take_rebuild()
        if request_id is None:
            return False
        snapshot_id = None
        self.observe(busy=True)
        try:
            if self.blocked():
                raise RebuildError("CDC_QUERY_REVIEW_REQUIRED")
            if self.partitions:
                self.broker.pause(self.partitions)
            self.state, snapshot_id = rebuild(
                self.admin,
                self.state,
                self.mapping,
                self.topics,
                request_id,
                materialize=self.materialize,
                validate_resume=self.resume_plan,
            )
            # The local generation is now durable. Failures below never roll
            # it back: startup resumes the validated local checkpoint.
            plan = self.resume_plan(self.state)
            for partition in plan:
                self.broker.seek(partition)
            self.broker.commit(offsets=plan, asynchronous=False)
            if self.partitions:
                self.broker.resume(self.partitions)
            self.maintenance = False
            self.error_code = None
            self.control.finish_rebuild(
                request_id, succeeded=True, snapshot_id=snapshot_id
            )
            self.control.update(last_success=observed_at())
            self.observe()
        except Exception as error:
            # A candidate/old connection may have been closed for publication.
            self.reopen_active_state()
            blocked = self.blocked()
            code = (
                "CDC_QUERY_REVIEW_REQUIRED"
                if blocked
                else (
                    "CDC_KAFKA_ACK_FAILED"
                    if self.state.get("rebuild_generation") == request_id
                    else "CDC_REBUILD_FAILED"
                )
            )
            diagnostic = (
                error.code if isinstance(error, RebuildError) else code
            )
            self.state.record_rebuild(
                request_id, "failed", snapshot_id, error=diagnostic
            )
            self.maintenance = True
            self.error_code = code
            self.control.finish_rebuild(
                request_id,
                succeeded=False,
                snapshot_id=snapshot_id,
                error_code=code,
            )
            self.observe()
        return True

    def process(self, records):
        """Process one bounded batch or halt fixed-source delivery."""
        self.observe(busy=True)
        self.state.last_raw_appended = 0
        counted = False
        try:
            result = self.process_batch(
                self.admin,
                self.state,
                records,
                self.mapping,
                self.topics,
                values=self.values,
            )
            self.appended += self.state.last_raw_appended
            counted = True
            offsets = {}
            for record in records:
                key = record["topic"], record["partition"]
                offsets[key] = max(offsets.get(key, -1), record["offset"] + 1)
            self.broker.commit(
                offsets=[
                    self.partition_factory(topic, partition, offset)
                    for (topic, partition), offset in offsets.items()
                ],
                asynchronous=False,
            )
            self.error_code = None
            self.control.update(last_success=observed_at())
            self.observe()
            return result
        except Exception:
            if not self.fixed:
                raise
            if not counted:
                self.appended += self.state.last_raw_appended
            self.maintenance = True
            self.error_code = "CDC_PROCESSING_FAILED"
            if counted:
                self.error_code = "CDC_KAFKA_ACK_FAILED"
            if self.partitions:
                self.broker.pause(self.partitions)
            self.observe()
            return None
