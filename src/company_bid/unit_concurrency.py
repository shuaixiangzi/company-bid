"""Bounded local unit scheduling; state commits stay in NodeContext's lock.

This is one CLI process, not a distributed Worker quota or lease protocol.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from contextlib import contextmanager
import threading


# Hard ceiling shared by model gateways in this CLI process. A deployed Worker
# still needs its own configured fair scheduler and cross-process quota.
_MODEL_REQUEST_SLOTS = threading.BoundedSemaphore(4)


@contextmanager
def model_request_slot(ctx):
    coordinator = getattr(ctx, "_parallel_coordinator", None)
    if coordinator is not None:
        coordinator.check()
    while not _MODEL_REQUEST_SLOTS.acquire(timeout=.1):
        if coordinator is not None:
            coordinator.check()
    released = False

    def release():
        nonlocal released
        if not released:
            released = True
            _MODEL_REQUEST_SLOTS.release()

    try:
        if coordinator is not None:
            coordinator.check()
        yield release
    finally:
        release()


class ParallelAdmissionStopped(RuntimeError):
    code = "PARALLEL_ADMISSION_STOPPED"


class UnitCoordinator:
    def __init__(self, unit_ids):
        self.unit_ids = tuple(unit_ids)
        self.positions = {uid: index for index, uid in enumerate(unit_ids)}
        self.condition = threading.Condition()
        self.completed = set()
        self.first_error = None

    def check(self):
        with self.condition:
            if self.first_error is not None:
                raise ParallelAdmissionStopped("A sibling failed; no new requests may be admitted.")

    def finish(self, index, error=None):
        with self.condition:
            self.completed.add(index)
            if error is not None and self.first_error is None:
                self.first_error = error
            self.condition.notify_all()

    def stop(self, error):
        with self.condition:
            if self.first_error is None:
                self.first_error = error
            self.condition.notify_all()

    def prior_ids(self, unit_id):
        index = self.positions[unit_id]
        with self.condition:
            while any(prior not in self.completed for prior in range(index)):
                self.check()
                self.condition.wait()
            self.check()
        return self.unit_ids[:index]


def run_parallel_units(ctx, jobs, concurrency):
    """Submit in source order; retain completed checkpoints and drain on error."""
    if type(concurrency) is not int or not 2 <= concurrency <= 4:
        raise ValueError("Parallel unit concurrency must be 2 through 4.")
    identities = [uid for uid, _ in jobs]
    if len(set(identities)) != len(identities):
        raise ValueError("Parallel units must have unique identities.")
    coordinator = UnitCoordinator(identities)
    results = [None] * len(jobs)

    def execute(index):
        uid, handler = jobs[index]
        child = ctx.parallel_child(coordinator, uid)
        try:
            value = handler(child)
        except BaseException as error:
            coordinator.finish(index, error)
            raise
        coordinator.finish(index)
        return value

    next_index = 0
    futures = {}
    # Exiting the pool waits for every admitted operation. The executor cannot
    # close its observer or fail the node while a response is still settling.
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="bid-window") as pool:
        try:
            while next_index < len(jobs) or futures:
                while next_index < len(jobs) and len(futures) < concurrency and coordinator.first_error is None:
                    context = copy_context()  # Each task gets the active node span.
                    futures[pool.submit(context.run, execute, next_index)] = next_index
                    next_index += 1
                if not futures:
                    break
                completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    index = futures.pop(future)
                    try:
                        results[index] = future.result()
                    except BaseException:
                        pass  # The original failure is latched by the worker.
        except BaseException as error:
            coordinator.stop(error)
            for future in futures:
                future.cancel()  # Running requests still drain on pool exit.
            raise
    if coordinator.first_error is not None:
        raise coordinator.first_error
    return results
