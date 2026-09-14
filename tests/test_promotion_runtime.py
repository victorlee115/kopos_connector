"""Process boundary regressions; the native integration checks use real V8."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
import resource
from types import SimpleNamespace
import subprocess
import tempfile
from unittest.mock import Mock

import pytest

from kopos_connector.pricing import runtime


SCHEDULE = {
    "timeZone": "Asia/Kuala_Lumpur", "weekdays": [1, 2, 3, 4, 5],
    "startDate": None, "endDate": None, "startMinute": 840, "endMinute": 1020,
}


def test_native_schedule_runs_in_a_fresh_process():
    assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE
    assert runtime.calculate("occurrences", {"schedule": SCHEDULE, "now": "2026-09-09T00:00:00Z"}) == [
        {"start": "2026-09-09T06:00:00.000Z", "end": "2026-09-09T09:00:00.000Z"},
        {"start": "2026-09-10T06:00:00.000Z", "end": "2026-09-10T09:00:00.000Z"},
        {"start": "2026-09-11T06:00:00.000Z", "end": "2026-09-11T09:00:00.000Z"},
    ]


@pytest.mark.parametrize("value", [float("nan"), {"text": "x" * 10001}, ["x" * 10000] * 200])
def test_invalid_or_oversized_input_never_spawns(value, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("must refuse before process creation"))
    with pytest.raises(runtime.PromotionRuntimeError):
        runtime.calculate("quote", value)


@pytest.mark.parametrize("failure", ["crash", "timeout", "stderr", "invalid_json", "wrong_artifact", "oversized"])
def test_native_failures_are_contained_without_an_accepted_price(failure, monkeypatch):
    manifest = json.loads(Path(runtime.__file__).with_name("runtime.json").read_text())

    def failed(*args, **kwargs):
        assert kwargs["env"] == {"PATH": runtime.os.defpath, "LANG": "C.UTF-8"}
        assert kwargs["timeout"] == 3
        assert args[0][1] == "-I"
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 3)
        response = json.dumps({"ok": True, "result": 123, "artifact": manifest["bundleSha256"]}).encode()
        if failure == "invalid_json":
            response = b"{"
        if failure == "wrong_artifact":
            response = b'{"ok":true,"result":123,"artifact":"wrong"}'
        if failure == "oversized":
            response = b"x" * (runtime.MAX_OUTPUT_BYTES + 1)
        return _completed(kwargs, response, b"failure" if failure == "stderr" else b"", -9 if failure == "crash" else 0)

    monkeypatch.setattr(runtime.subprocess, "run", failed)
    with pytest.raises(runtime.PromotionRuntimeError):
        runtime.calculate("schedule", SCHEDULE)
    assert runtime._SLOTS.acquire(blocking=False)
    assert runtime._SLOTS.acquire(blocking=False)
    runtime._SLOTS.release()
    runtime._SLOTS.release()


def test_native_validation_failure_does_not_poison_next_request():
    with pytest.raises(runtime.PromotionRuntimeError, match="weekday"):
        runtime.calculate("schedule", {**SCHEDULE, "weekdays": []})
    assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE


def test_busy_is_not_queued_without_a_bound():
    runtime._SLOTS.acquire()
    runtime._SLOTS.acquire()
    try:
        with pytest.raises(runtime.PromotionRuntimeError, match="busy"):
            runtime.calculate("schedule", SCHEDULE)
    finally:
        runtime._SLOTS.release()
        runtime._SLOTS.release()


def _response(result=None, **fields):
    artifact = json.loads(Path(runtime.__file__).with_name("runtime.json").read_text())["bundleSha256"]
    return {"ok": True, "artifact": artifact, "result": result, **fields}


def _completed(options, stdout, stderr=b"", returncode=0):
    options["stdout"].write(stdout)
    options["stderr"].write(stderr)
    return SimpleNamespace(returncode=returncode, stdout=None, stderr=None)


@pytest.mark.parametrize("operation,response", [
    ("quote", _response({"status": "complete"})),
    ("quote", _response({"kernelVersion": 2, "status": "incomplete", "reason": "input_limit", "searchNodes": 0, "totalDiscountSen": 123})),
    ("quote", _response({"kernelVersion": 2, "status": "incomplete", "reason": "unknown", "searchNodes": 0})),
    ("quote", _response({"kernelVersion": 1, "status": "incomplete", "reason": "input_limit", "searchNodes": 0})),
    ("schedule", _response(None)),
    ("schedule", _response(123)),
    ("schedule", _response({**SCHEDULE, "weekdays": [True]})),
    ("schedule", _response({**SCHEDULE, "weekdays": [1, 1]})),
    ("schedule", _response({**SCHEDULE, "unexpected": "field"})),
    ("occurrences", _response([{"start": "tomorrow", "end": "later"}])),
    ("occurrences", _response([{"start": "2026-09-09T06:00:00.000Z"}])),
    ("identity", _response({"canonical": "{}", "hash": "wrong", "version": "wrong"})),
    ("identity", _response({"canonical": "{}", "hash": "a" * 64})),
    ("basket_totals", _response([True])),
    ("basket_totals", _response([-1])),
    ("basket_totals", _response([1.5])),
    ("basket_totals", _response([9007199254740992])),
    ("basket_totals", _response([float("nan")])),
    ("basket_totals", _response([float("inf")])),
    ("basket_totals", _response([0] * 1276)),
    ("schedule", {key: value for key, value in _response(SCHEDULE).items() if key != "result"}),
    ("schedule", _response(SCHEDULE, ok=1)),
    ("schedule", _response(SCHEDULE, message="contradictory success")),
    ("schedule", _response(SCHEDULE, ok=False, message="failure with a price")),
])
def test_malformed_runtime_result_is_never_accepted(operation, response, monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: _completed(k, json.dumps(response).encode()))
    with pytest.raises(runtime.PromotionRuntimeError, match="response"):
        runtime.calculate(operation, {})
    assert runtime._SLOTS.acquire(blocking=False)
    assert runtime._SLOTS.acquire(blocking=False)
    runtime._SLOTS.release()
    runtime._SLOTS.release()


def test_response_nesting_failure_is_contained(monkeypatch):
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: _completed(k, b"[" * 1500 + b"0" + b"]" * 1500))
    with pytest.raises(runtime.PromotionRuntimeError, match="response"):
        runtime.calculate("quote", {})


def test_process_start_failure_is_contained_and_next_request_runs(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("synthetic process capacity failure")

    with monkeypatch.context() as isolated:
        isolated.setattr(runtime.subprocess, "run", unavailable)
        with pytest.raises(runtime.PromotionRuntimeError, match="could not start"):
            runtime.calculate("schedule", SCHEDULE)
    assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE


def test_native_identity_and_empty_batch_preserve_exact_results():
    import hashlib

    canonical = '{"a":1,"b":"茶"}'
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    assert runtime.calculate("identity", {"b": "茶", "a": 1}) == {
        "canonical": canonical, "hash": digest, "version": "KOPOS-PROMO-" + digest[:16].upper(),
    }
    assert runtime.calculate("basket_totals", {"input": {}, "baskets": []}) == []


def test_native_incomplete_result_never_contains_a_partial_price():
    result = runtime.calculate("quote", {
        "kernelVersion": 2, "cartItems": [], "menuItems": [], "snapshot": None,
        "nowIso": "2026-09-09T00:00:00Z", "posProfile": "Test POS", "networkOnline": True,
    })
    assert result == {"kernelVersion": 2, "status": "incomplete", "reason": "snapshot_unavailable", "searchNodes": 0}


@pytest.mark.parametrize("raw_number", ["1e400", "-1e400", "NaN", "Infinity", "-Infinity"])
def test_nonfinite_response_numbers_are_rejected_during_json_decoding(raw_number, monkeypatch):
    response = json.dumps(_response(SCHEDULE)).replace('"startDate": null', '"startDate": ' + raw_number)
    monkeypatch.setattr(runtime.subprocess, "run", lambda *a, **k: _completed(k, response.encode()))
    with pytest.raises(runtime.PromotionRuntimeError, match="response"):
        runtime.calculate("schedule", SCHEDULE)


def test_native_maximum_cart_shape_passes_and_invalid_nested_money_is_refused(monkeypatch):
    from copy import deepcopy

    now = "2030-01-01T00:00:00Z"
    request = {
        "kernelVersion": 2, "nowIso": now, "posProfile": "Test POS", "networkOnline": True,
        "menuItems": [{"id": "M", "categoryId": "Drinks"}],
        "snapshot": {"kernelVersion": 2, "source": "published", "posProfile": "Test POS",
                     "snapshotVersion": "test", "snapshotHash": "test", "downloadedAt": now,
                     "publishedAt": now, "effectiveFrom": now, "promotions": []},
        "cartItems": [{"id": f"L-{index}", "menuItemId": "M", "quantity": 10, "unitPriceSen": 501,
                       "modifierTotalSen": 10, "modifiers": [{"name": "Tea 茶", "priceSen": 10}],
                       "discountAmountSen": 5, "note": "Keep this extra field"} for index in range(100)],
    }
    result = runtime.calculate("quote", request)
    assert result["status"] == "complete"
    assert len(result["items"]) == 100
    assert result["totalDiscountSen"] == 0
    for row in result["items"]:
        assert row["lineTotalSen"] == 5105
        assert row["note"] == "Keep this extra field"
        assert row["modifiers"] == [{"name": "Tea 茶", "priceSen": 10}]
    for field in ("unitPriceSen", "modifierTotalSen", "discountAmountSen", "lineTotalSen"):
        for invalid in (True, -1, 0.5, 9007199254740992):
            malformed = deepcopy(result)
            malformed["items"][0][field] = invalid
            with monkeypatch.context() as isolated:
                isolated.setattr(runtime.subprocess, "run", lambda *a, **k: _completed(k, json.dumps(_response(malformed)).encode()))
                with pytest.raises(runtime.PromotionRuntimeError, match="response"):
                    runtime.calculate("quote", request)
    assert runtime.calculate("quote", request) == result


@pytest.mark.parametrize("descriptor", [1, 2])
def test_real_worker_os_limit_caps_each_output_file(descriptor):
    # Execute the actual worker's startup in a disposable child, never import
    # its resource settings into this pytest/Frappe parent.
    worker = Path(runtime.__file__).with_name("worker.py")
    code = (f"import runpy, os; runpy.run_path({str(worker)!r}, run_name='limit_probe')\n"
            f"for _ in range(128): os.write({descriptor}, b'x' * 65536)")
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run([runtime.sys.executable, "-I", "-c", code], stdout=output, stderr=errors,
                                timeout=runtime.REQUEST_TIMEOUT_SECONDS, check=False)
        assert result.returncode != 0, "The OS must stop an oversized worker write"
        assert os.fstat(output.fileno()).st_size <= runtime.MAX_OUTPUT_BYTES + 1
        assert os.fstat(errors.fileno()).st_size <= runtime.MAX_OUTPUT_BYTES + 1


@pytest.mark.parametrize("failure", [None, "stdout_flood", "stderr_flood", "killed", "timeout"])
def test_real_process_cleanup_and_bounded_parent_reads(failure, monkeypatch):
    real_file, real_run, real_popen = tempfile.TemporaryFile, subprocess.run, subprocess.Popen
    captures, processes = [], []
    parent_limits = {kind: resource.getrlimit(kind) for kind in (resource.RLIMIT_FSIZE, resource.RLIMIT_CORE)}

    @contextmanager
    def capture():
        with real_file() as stream:
            observed = Mock(wraps=stream)
            captures.append((stream, observed))
            assert os.fstat(stream.fileno()).st_nlink == 0
            assert os.fstat(stream.fileno()).st_mode & 0o077 == 0
            yield observed

    def track_process(*args, **options):
        process = real_popen(*args, **options)
        processes.append(process)
        return process

    def run(args, **options):
        assert options["stdout"] is not subprocess.PIPE
        assert options["stderr"] is not subprocess.PIPE
        assert "preexec_fn" not in options
        assert options["timeout"] == 3
        if failure:
            worker = Path(runtime.__file__).with_name("worker.py")
            code = f"import runpy, os, signal, time; runpy.run_path({str(worker)!r}, run_name='fault_probe')\n"
            if failure.endswith("flood"):
                descriptor = 1 if failure == "stdout_flood" else 2
                code += f"for _ in range(128): os.write({descriptor}, b'x' * 65536)"
            elif failure == "killed":
                code += "os.kill(os.getpid(), signal.SIGKILL)"
            else:
                code += "time.sleep(10)"
            args = [args[0], "-I", "-c", code]
        try:
            return real_run(args, **options)
        finally:
            for stream, _observed in captures:
                assert os.fstat(stream.fileno()).st_size <= runtime.MAX_OUTPUT_BYTES + 1

    with monkeypatch.context() as isolated:
        isolated.setattr(runtime.tempfile, "TemporaryFile", capture)
        isolated.setattr(runtime.subprocess, "Popen", track_process)
        isolated.setattr(runtime.subprocess, "run", run)
        if failure:
            with pytest.raises(runtime.PromotionRuntimeError):
                runtime.calculate("schedule", SCHEDULE)
        else:
            assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE
    assert len(captures) == 2
    for index, (stream, observed) in enumerate(captures):
        assert stream.closed
        assert all(call.args == ((runtime.MAX_OUTPUT_BYTES + 1) if index == 0 else 1,)
                   for call in observed.read.call_args_list)
        assert observed.read.call_count == (0 if failure == "timeout" else 1)
    assert len(processes) == 1
    assert processes[0].returncode is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(processes[0].pid, os.WNOHANG)
    assert {kind: resource.getrlimit(kind) for kind in parent_limits} == parent_limits
    assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE


@pytest.mark.parametrize("which", [1, 2])
def test_temporary_storage_failure_is_contained_and_releases_resources(which, monkeypatch):
    real_file = tempfile.TemporaryFile
    opened = []

    def unavailable():
        if len(opened) + 1 == which:
            raise OSError("Synthetic temporary storage unavailable")
        stream = real_file()
        opened.append(stream)
        return stream

    with monkeypatch.context() as isolated:
        isolated.setattr(runtime.tempfile, "TemporaryFile", unavailable)
        isolated.setattr(runtime.subprocess, "run", lambda *a, **k: pytest.fail("must not spawn"))
        with pytest.raises(runtime.PromotionRuntimeError, match="could not start"):
            runtime.calculate("schedule", SCHEDULE)
    assert all(stream.closed for stream in opened)
    assert runtime.calculate("schedule", SCHEDULE) == SCHEDULE


def test_worker_preserves_a_lower_inherited_file_limit():
    worker = Path(runtime.__file__).with_name("worker.py")
    code = ("import resource, runpy, json; resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 8192)); "
            f"runpy.run_path({str(worker)!r}, run_name='limit_probe'); "
            "print(json.dumps(resource.getrlimit(resource.RLIMIT_FSIZE)))")
    result = subprocess.run([runtime.sys.executable, "-I", "-c", code], capture_output=True,
                            timeout=runtime.REQUEST_TIMEOUT_SECONDS, check=True)
    assert json.loads(result.stdout) == [4096, 4096]
    assert result.stderr == b""
