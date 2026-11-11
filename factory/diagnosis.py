"""Turn a provider failure into something an operator can act on.

A provider rejection used to surface as `ValueError: execution failed; inspect task evidence and
readiness`. That is safe — no secret leaks — but it is useless: the actual text from the provider
already said "This API key has expired", and the operator had to dig through evidence files to
find it.

The rule here is narrow: recognise the failures that have a **known, actionable cause**, and
describe that cause in terms of what to do. Everything else keeps the generic message, because
inventing an explanation for an unknown failure is worse than admitting there isn't one.

Never echoed: the key itself, and any token-shaped fragment. The messages below are written from
a fixed vocabulary, not by quoting the provider, so a provider that echoes a credential into its
error cannot leak it through this path.
"""
import re

# Provider failures worth naming. Each entry is (pattern, operator-facing explanation).
# Ordered: the first match wins, so put the specific cases before the general ones.
KNOWN_FAILURES = (
    (re.compile(r"api_key_expired|key has expired|expired api key", re.I),
     "API key provider sudah kedaluwarsa. Buat key baru di konsol provider, perbarui "
     "OPENCODE_API_KEY, lalu jalankan ulang pekerjaan ini."),
    (re.compile(r"authentication_error|invalid_api_key|incorrect api key|401\b", re.I),
     "API key provider ditolak. Periksa OPENCODE_API_KEY: key salah, sudah dicabut, atau "
     "milik akun lain."),
    (re.compile(r"insufficient_quota|quota exceeded|billing|credit balance", re.I),
     "Kuota atau saldo provider habis. Isi ulang kredit lalu coba lagi."),
    (re.compile(r"rate_limit|too many requests|429\b", re.I),
     "Provider membatasi laju permintaan. Tunggu sebentar lalu coba lagi."),
    (re.compile(r"model_not_found|no such model|unknown model|404.*model", re.I),
     "Model yang diminta tidak tersedia untuk akun ini. Periksa FACTORY_MODEL."),
    (re.compile(r"context_length|maximum context|too many tokens", re.I),
     "Permintaan melebihi batas konteks model. Perkecil brief atau rencanakan lebih sedikit "
     "tugas sekaligus."),
    (re.compile(r"connection refused|name resolution|getaddrinfo|network is unreachable|"
                r"temporary failure in name resolution", re.I),
     "Provider tidak bisa dihubungi. Periksa koneksi jaringan server lalu coba lagi."),
)

# A worker that runs out of time is not a provider problem, so it is matched before the more
# general timeout wording above could ever swallow it, and it names the knob to raise. Measured
# case: a repair worker was killed at 600.5s because it inherited the one-shot call budget while
# its real budget is 1800s.
TIMEOUT_FAILURE = re.compile(r"exceeded (?:its )?(\d+)\s*s deadline", re.I)


def describe_timeout(exc, worker_timeout=None) -> str:
    """An explanation for a worker killed by a deadline, or an empty string.

    Separate from `explain` because it needs the configured budget to say anything useful:
    "timed out" alone does not tell an operator whether the task is too large or the limit is
    too small, and that is the only decision they have to make.
    """
    message = str(exc)
    reason = getattr(exc, "reason", "")
    match = TIMEOUT_FAILURE.search(message)
    if not match and reason != "timeout":
        return ""
    limit = match.group(1) if match else (str(worker_timeout) if worker_timeout else "")
    detail = f" setelah {limit} detik" if limit else ""
    return ("Pekerjaan model dihentikan karena melewati batas waktu" + detail + ". Tugas ini "
            "kemungkinan lebih besar daripada anggarannya: naikkan FACTORY_WORKER_TIMEOUT "
            "(batas satu pekerja harness) atau FACTORY_JOB_TIMEOUT (batas seluruh pekerjaan), "
            "atau pecah tugasnya menjadi lebih kecil. Periksa juga sisa perubahan di product "
            "repository sebelum mengulang.")


def explain(text) -> str:
    """An operator-facing sentence for a provider failure, or an empty string.

    An empty result means "no known cause", and the caller must keep its generic message rather
    than guess. Returning a confident-sounding wrong diagnosis is the failure mode to avoid.
    """
    if not text:
        return ""
    haystack = str(text)
    for pattern, message in KNOWN_FAILURES:
        if pattern.search(haystack):
            return message
    return ""


def describe(exc) -> str:
    """The best available explanation for a failed job.

    Looks at the exception's own text and at `exc.output`, because the provider's error arrives
    in the process output rather than in the exception message for a model call that produced no
    text.
    """
    parts = [str(exc)]
    output = getattr(exc, "output", "")
    if output:
        parts.append(str(output)[-4000:])
    # A deadline is checked before the provider vocabulary: a killed worker's output is usually
    # just its own progress stream, and matching "timeout" inside that stream would blame the
    # network for a budget that was simply too small.
    deadline = describe_timeout(exc)
    if deadline:
        return deadline
    return explain("\n".join(parts))
