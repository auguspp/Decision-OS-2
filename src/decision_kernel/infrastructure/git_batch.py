"""One command's bounded, lazy ``git cat-file --batch-command`` process.

The caller owns repository admission, Git options and the restricted environment.
This module accepts only exact object ids and implements Git's small binary wire
format. A broken exchange ends the session; it is never restarted implicitly.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
from threading import Event, Lock, Thread, current_thread

MAX_BLOB_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 128
MAX_STDERR_BYTES = 8192
_MAX_OBJECT_SIZE = (1 << 64) - 1
_CLOSE_SECONDS = 0.5
_OID = re.compile(r"[0-9a-f]{40}")
_HEADER = re.compile(rb"([0-9a-f]{40}) (blob|tree|commit|tag) (0|[1-9][0-9]*)\n")


class GitBatchUnavailable(Exception):
    """The session cannot answer; reasons contain no child output or paths."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GitObjectBatch:
    def __init__(self, command: list[str], *, env: dict):
        self._command, self._env = list(command), dict(env)
        self._query_lock, self._state_lock, self._close_lock = Lock(), Lock(), Lock()
        self._process = None
        self._started = self._closed = False
        self._failure = None
        self._worker = self._stderr_reader = self._done = None
        self._processes = self._requests = 0

    def stats(self) -> dict:
        """Successful process creations and attempted protocol requests."""
        with self._state_lock:
            return {"processes": self._processes, "requests": self._requests}

    def _check(self):
        with self._state_lock:
            reason = self._failure or ("GIT_BATCH_CLOSED" if self._closed else None)
        if reason is not None:
            raise GitBatchUnavailable(reason)

    def _poison(self, reason):
        with self._state_lock:
            if self._failure is None and not self._closed:
                self._failure = reason
            process, done = self._process, self._done
        # Waking the caller also bounds failures found by the stderr reader.
        if done is not None:
            done.set()
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def _start(self):
        self._check()
        if self._started:
            return
        self._started = True
        process = reader = None
        try:
            try:
                process = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                    env=self._env, shell=False)
            except (OSError, ValueError):
                raise GitBatchUnavailable("GIT_BATCH_START_FAILED") from None
            with self._state_lock:
                self._process = process
                self._processes += 1
            self._check()
            reader = Thread(target=self._drain_stderr, args=(process,), daemon=True)
            with self._state_lock:
                self._stderr_reader = reader
            reader.start()
            self._check()
        except BaseException:
            # close() may have seen no process while Popen was still creating
            # one. The creator owns cleanup until the process is handed over.
            if process is not None:
                self._dispose(process, None, reader, time.monotonic() + _CLOSE_SECONDS)
            raise

    def _drain_stderr(self, process):
        count = 0
        try:
            while True:
                part = process.stderr.read(min(4096, MAX_STDERR_BYTES - count + 1))
                if not part:
                    return
                count += len(part)
                if count > MAX_STDERR_BYTES:
                    self._poison("GIT_BATCH_OUTPUT_LIMIT")
                    return
        except (OSError, ValueError):
            self._poison("GIT_BATCH_STREAM_FAILED")
        finally:
            self._close_stream(process.stderr)

    @staticmethod
    def _close_stream(stream):
        try:
            stream.close()
        except BaseException:
            pass

    @staticmethod
    def _read_exact(stream, size):
        result = bytearray()
        while len(result) < size:
            part = stream.read(min(65536, size - len(result)))
            if not isinstance(part, bytes) or not part or len(part) > size - len(result):
                raise GitBatchUnavailable("GIT_BATCH_STREAM_FAILED")
            result.extend(part)
        return bytes(result)

    @classmethod
    def _header(cls, stream):
        result = bytearray()
        for _ in range(MAX_HEADER_BYTES):
            part = cls._read_exact(stream, 1)
            result.extend(part)
            if part == b"\n":
                return bytes(result)
        raise GitBatchUnavailable("GIT_BATCH_OUTPUT_LIMIT")

    def _exchange(self, oid, expected_size, result, done):
        process = self._process
        try:
            self._check()
            command = ("info " if expected_size is None else "contents ") + oid + "\n"
            request, written = command.encode("ascii"), 0
            with self._state_lock:
                self._requests += 1
            # A stalled write is supervised by the caller's deadline too.
            while written < len(request):
                count = process.stdin.write(request[written:])
                if type(count) is not int or count <= 0 or count > len(request) - written:
                    raise GitBatchUnavailable("GIT_BATCH_STREAM_FAILED")
                written += count
            header = self._header(process.stdout)
            if header == oid.encode("ascii") + b" missing\n":
                answer = None
            else:
                match = _HEADER.fullmatch(header)
                if match is None or match[1].decode("ascii") != oid:
                    raise GitBatchUnavailable("GIT_BATCH_PROTOCOL")
                kind, size = match[2].decode("ascii"), int(match[3])
                if size > _MAX_OBJECT_SIZE:
                    raise GitBatchUnavailable("GIT_BATCH_PROTOCOL")
                if expected_size is None:
                    answer = (kind, size)
                else:
                    if kind != "blob" or size != expected_size:
                        raise GitBatchUnavailable("GIT_BATCH_PROTOCOL")
                    answer = self._read_exact(process.stdout, size)
                    if self._read_exact(process.stdout, 1) != b"\n":
                        raise GitBatchUnavailable("GIT_BATCH_PROTOCOL")
                    digest = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0" + answer)
                    if digest.hexdigest() != oid:
                        raise GitBatchUnavailable("GIT_BATCH_OID_MISMATCH")
            result.append(answer)
        except GitBatchUnavailable as error:
            self._poison(error.reason)
        except Exception:
            self._poison("GIT_BATCH_STREAM_FAILED")
        finally:
            # On an interrupted exchange the worker owns its raw pipe handles
            # until the killed child unblocks them. No buffered close can wait
            # in the caller for a writer/reader lock.
            with self._state_lock:
                stopped = self._closed or self._failure is not None
            if stopped:
                self._close_stream(process.stdin)
                self._close_stream(process.stdout)
            done.set()

    def _query(self, oid, expected_size, deadline):
        if not isinstance(oid, str) or _OID.fullmatch(oid) is None:
            raise GitBatchUnavailable("GIT_BATCH_INVALID_OID")
        try:
            valid_deadline = type(deadline) in (int, float) and math.isfinite(deadline)
        except OverflowError:
            valid_deadline = False
        if not valid_deadline:
            raise GitBatchUnavailable("GIT_BATCH_INVALID_DEADLINE")
        acquired = False
        try:
            self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._query_lock.acquire(timeout=remaining):
                raise GitBatchUnavailable("GIT_BATCH_TIMEOUT")
            acquired = True
            self._check()
            self._start()
            if self._process.poll() is not None:
                raise GitBatchUnavailable("GIT_BATCH_EXITED")
            if time.monotonic() >= deadline:
                raise GitBatchUnavailable("GIT_BATCH_TIMEOUT")
            result, done = [], Event()
            worker = Thread(target=self._exchange,
                args=(oid, expected_size, result, done), daemon=True)
            with self._state_lock:
                self._worker, self._done = worker, done
            worker.start()
            if not done.wait(timeout=max(0, deadline - time.monotonic())):
                raise GitBatchUnavailable("GIT_BATCH_TIMEOUT")
            self._check()
            worker.join(timeout=max(0, deadline - time.monotonic()))
            if worker.is_alive() or time.monotonic() >= deadline:
                raise GitBatchUnavailable("GIT_BATCH_TIMEOUT")
            if not result or self._process.poll() is not None:
                raise GitBatchUnavailable("GIT_BATCH_EXITED")
            return result[0]
        except GitBatchUnavailable as error:
            self._poison(error.reason)
            self.close()
            raise
        except (OSError, RuntimeError, ValueError):
            self._poison("GIT_BATCH_STREAM_FAILED")
            self.close()
            raise GitBatchUnavailable("GIT_BATCH_STREAM_FAILED") from None
        except BaseException:
            self.close()
            raise
        finally:
            if acquired:
                self._query_lock.release()

    def info(self, oid, *, deadline) -> tuple[str, int] | None:
        return self._query(oid, None, deadline)

    def blob(self, oid, expected_size, *, deadline) -> bytes | None:
        if type(expected_size) is not int or not 0 <= expected_size <= MAX_BLOB_BYTES:
            raise GitBatchUnavailable("GIT_BATCH_INVALID_SIZE")
        return self._query(oid, expected_size, deadline)

    def close(self) -> None:
        """Stop once, reap and join within a shared small cleanup budget."""
        with self._state_lock:
            self._closed = True
            done = self._done
        if done is not None:
            done.set()
        if not self._close_lock.acquire(blocking=False):
            return
        try:
            deadline = time.monotonic() + _CLOSE_SECONDS
            with self._state_lock:
                process, worker, reader = self._process, self._worker, self._stderr_reader
            if process is not None:
                self._dispose(process, worker, reader, deadline)
        except BaseException:
            # Cleanup must not hide the original failure or KeyboardInterrupt.
            pass
        finally:
            self._close_lock.release()

    def _dispose(self, process, worker, reader, deadline):
        try:
            process.kill()
        except BaseException:
            pass
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except BaseException:
            pass
        for thread in (worker, reader):
            try:
                if thread is not None and thread.ident is not None and thread is not current_thread():
                    thread.join(timeout=max(0, deadline - time.monotonic()))
            except BaseException:
                pass
        # Active raw pipe owners close themselves after kill. Only close idle
        # handles here, so cleanup never waits for a thread's I/O lock.
        if worker is None or not worker.is_alive():
            self._close_stream(process.stdin)
            self._close_stream(process.stdout)
        if reader is None or not reader.is_alive():
            self._close_stream(process.stderr)
