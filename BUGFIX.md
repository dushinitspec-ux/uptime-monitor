# Bugs found and fixed

Real bugs I hit while building this project, in the order I found them. All three showed up
when I ran the checks against the real network of my own computer (Windows 11 with a
Russian locale) instead of only against made-up sample output.

---

## 1. Ping latency was never measured on a non-English Windows

**What broke.** A device checked by ping only (my home router) was reported as UP, but its
latency was always empty:

```
router True None ()          # ok, latency_ms, problems
```

**Root cause.** On this machine `ping` prints its reply in Russian, in the console code page
cp866: `Ответ от 127.0.0.1: число байт=32 время<1мс TTL=128`. To stay independent of code
pages, the checker decodes the output as ASCII, which keeps `TTL=128` but turns every
Russian letter into a replacement character. The latency regex looked for the unit `ms` or
`мс`, and the Cyrillic `мс` no longer existed after decoding, so it never matched.

**Fix.** Do not depend on the word for "milliseconds" at all. Linux and English Windows are
matched by `time=12.3 ms`; any other Windows language by the number that comes right before
` TTL=`, which every localized version prints:

```python
# before
_LATENCY = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\s*(?:ms|мс)", re.IGNORECASE)
...
match = _LATENCY.search(output) if ok else None
```

```python
# after
_LATENCY_LATIN = re.compile(r"time[=<]\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
_LATENCY_BEFORE_TTL = re.compile(r"[=<]\s*(\d+(?:[.,]\d+)?)\S*\s+TTL=")
...
match = _LATENCY_LATIN.search(output) or _LATENCY_BEFORE_TTL.search(output)
```

The tests now feed the parser the real Russian output, encoded to cp866 and decoded the same
way the checker does, next to English Windows and Linux samples.

**Lesson.** Test parsers with real output from real systems, not with what you expect
the output to look like.

---

## 2. Error messages in Russian on an English status page

**What broke.** The same run reported a closed port like this:

```
('tcp/1: Удаленный компьютер отклонил это сетевое подключение',)
```

That text would go into the English status page and into Telegram alerts.

**Root cause.** The checker used `exc.strerror`, and on Windows that message comes from the
operating system in the system language.

**Fix.** Map the errors that matter to short, fixed English reasons and keep the OS text out:

```python
# before
except OSError as exc:
    return False, None, f"tcp/{port}: {exc.strerror or exc.__class__.__name__}"
```

```python
# after
except OSError as exc:
    return False, None, f"tcp/{port}: {describe_error(exc)}"

def describe_error(exc: OSError) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, socket.gaierror):
        return "host name not found"
    ...
```

Now: `tcp/1: connection refused`, `tcp/80: host name not found`.

---

## 3. A refused connection was reported as a timeout on Windows

**What broke.** The test "a closed port gives *connection refused*" failed:

```
AssertionError: assert 'tcp/63925: timed out' == 'tcp/63925: connection refused'
```

**Root cause.** I measured it: Windows reports a refused connection only after
**2.05 seconds**, both on localhost and on the LAN router; Linux reports it at once. Windows
retries the connection before giving up. The test used a 2-second timeout, so the timeout
always won. The check still failed correctly, but with the wrong reason. "Refused" means the
device is alive and the service is down. "Timed out" means the device or the network is not
answering. For the person fixing it, those are two different problems.

**Fix.** The test waits 5 seconds, and `.env.example` explains why `TCP_TIMEOUT_S` must stay
at 3 or more on Windows:

```python
# before
ok, _, problem = await tcp_connect("127.0.0.1", port, timeout_s=2)
```

```python
# after
# Windows reports a refused connection only after ~2 s (it retries), so wait longer.
ok, _, problem = await tcp_connect("127.0.0.1", port, timeout_s=5)
```

**Lesson.** When a test fails "for no reason", measure before changing the code. Here the
code was right, and the wrong assumption was about the operating system.
