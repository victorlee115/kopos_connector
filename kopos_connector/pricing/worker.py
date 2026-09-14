"""Private single-request process. Never import this into a Frappe process."""
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import resource
import sys


MAX_INPUT = 1_048_576
MAX_OUTPUT = 2_097_152

# This module runs only in the disposable Unix worker. Apply limits before
# importing the native bridge; never use preexec_fn in a threaded ERP process.
_file_limit = min(limit for limit in (MAX_OUTPUT + 1, *resource.getrlimit(resource.RLIMIT_FSIZE))
                  if limit != resource.RLIM_INFINITY)
resource.setrlimit(resource.RLIMIT_FSIZE, (_file_limit, _file_limit))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

from py_mini_racer import mini_racer


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("Promotion input exceeds the allowed size")
    request = json.loads(raw)
    functions = {
        "quote": "JiJiPromotionRuntime.quotePromotions",
        "basket_totals": "JiJiPromotionRuntime.quotePromotionBasketTotals",
        "schedule": "JiJiPromotionRuntime.readPromotionSchedule",
        "occurrences": "JiJiPromotionRuntime.nextPromotionOccurrences",
        "identity": "JiJiPromotionRuntime.canonicalPromotionContent",
    }
    function = functions[request["operation"]]
    directory = Path(__file__).resolve().parent
    bundle = (directory / "runtime.js").read_bytes()
    manifest = json.loads((directory / "runtime.json").read_text())
    artifact = hashlib.sha256(bundle).hexdigest()
    if artifact != manifest["bundleSha256"] or len(bundle) != manifest["bundleBytes"]:
        raise ValueError("Packaged pricing identity does not match")
    if importlib.metadata.version("mini-racer") != manifest["miniRacerVersion"]:
        raise ValueError("Promotion runtime version does not match")
    diagnostics = []
    sys.unraisablehook = lambda event: diagnostics.append(str(event.exc_value))

    def execute(loop):
        with mini_racer(event_loop=loop) as context:
            context.set_hard_memory_limit(64 * 1024 * 1024)
            context.eval(bundle.decode(), timeout_sec=0.5)
            payload = request["payload"]
            if request["operation"] == "occurrences":
                return context.call(function, payload["schedule"], payload["now"], timeout_sec=0.5)
            return context.call(function, payload, timeout_sec=0.5)

    async def run():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, event: diagnostics.append(str(event.get("exception") or event.get("message"))))
        # Synchronous MiniRacer waits on the supplied loop; never call it on
        # that loop's own thread. Context cleanup precedes loop shutdown.
        return await asyncio.to_thread(execute, loop)

    try:
        result = asyncio.run(run())
        if diagnostics:
            raise ValueError("Promotion runtime cleanup failed")
        if request["operation"] == "identity":
            # The identical shared JS library owns canonical bytes. The host's
            # standard SHA-256 avoids adding a browser TextEncoder polyfill to V8.
            digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
            result = {"canonical": result, "hash": digest, "version": "KOPOS-PROMO-" + digest[:16].upper()}
        response = {"ok": True, "result": result, "artifact": artifact}
    except Exception as error:
        response = {"ok": False, "message": str(error)[:600], "artifact": artifact}
    encoded = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_OUTPUT:
        raise ValueError("Promotion output exceeds the allowed size")
    sys.stdout.buffer.write(encoded)


if __name__ == "__main__":
    main()
