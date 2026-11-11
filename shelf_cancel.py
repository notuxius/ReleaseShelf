"""Cooperative job cancellation and interruptible reads from publishers."""

import hashlib
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from queue import Empty, Full, Queue
from threading import Event, Thread


_cancel_event = ContextVar("release_cancel_event", default=None)


class JobCanceled(Exception):
    pass


def check_canceled():
    event = _cancel_event.get()
    if event is not None and event.is_set():
        raise JobCanceled()


@contextmanager
def cancel_scope(event):
    token = _cancel_event.set(event)
    try:
        check_canceled()
        yield
    finally:
        _cancel_event.reset(token)


def read_chunks(stream):
    read = getattr(stream, "read1", stream.read)
    while True:
        check_canceled()
        block = read(1024 * 1024)
        if not block:
            return
        yield block


def file_sha256(stream):
    if _cancel_event.get() is None:
        return hashlib.file_digest(stream, "sha256").hexdigest()
    checksum = hashlib.sha256()
    for block in read_chunks(stream):
        checksum.update(block)
    return checksum.hexdigest()


def publisher_items(read):
    """Keep blocked network reads off the job thread. Only the job thread writes the cache."""
    event = _cancel_event.get()
    if event is None:
        yield from read()
        return

    channel = Queue(maxsize=2)
    finished = object()
    stopped = Event()

    def send(item):
        while not stopped.is_set() and not event.is_set():
            try:
                channel.put(item, timeout=0.05)
                return True
            except Full:
                continue
        return False

    def produce():
        try:
            check_canceled()
            for item in read():
                if not send(item):
                    return
        except BaseException as exc:
            send(exc)
        else:
            send(finished)

    check_canceled()
    context = copy_context()
    Thread(target=context.run, args=(produce,), daemon=True, name="Publisher read").start()
    try:
        while True:
            check_canceled()
            try:
                item = channel.get(timeout=0.05)
            except Empty:
                continue
            check_canceled()
            if item is finished:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stopped.set()
