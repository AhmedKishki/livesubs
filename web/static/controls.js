// Live operator settings. Only server-confirmed state describes recognition.
export function bindSessionControls(sessionId, element, fetcher = fetch) {
  const speaker = element("speaker-language");
  const fallback = element("default-language");
  const status = element("recognition-state");
  const error = element("settings-error");
  let confirmed = null;
  let pending = false;

  function render() {
    if (!confirmed) return;
    speaker.value = confirmed.speaker;
    fallback.value = confirmed.default_language;
    const recognition = confirmed.recognition ?? {};
    const detected = recognition.detected;
    status.textContent = `${recognition.state ?? "unknown"} · requested: ${recognition.requested ?? confirmed.speaker}` +
      ` · active: ${recognition.active ?? "none"}` +
      (detected ? ` · last detected: ${detected.language} (segment ${detected.seq})` : " · last detected: none") +
      (recognition.error ? ` · ${recognition.error}` : "");
  }

  function reconcile(settings) {
    if (!settings || (confirmed && settings.revision < confirmed.revision)) return;
    confirmed = settings;
    if (!pending) render();
  }

  async function change(key, value) {
    if (pending) return;
    pending = true;
    speaker.disabled = fallback.disabled = true;
    error.textContent = "";
    try {
      const response = await fetcher(`/api/session/${encodeURIComponent(sessionId)}/settings`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [key]: value }),
      });
      if (!response.ok) throw new Error("settings rejected");
      reconcile(await response.json());
    } catch {
      error.textContent = "Change not applied. Check the connection and try again.";
    } finally {
      pending = false;
      speaker.disabled = fallback.disabled = false;
      render();
    }
  }

  speaker.addEventListener("change", () => change("speaker", speaker.value));
  fallback.addEventListener("change", () => change("default_language", fallback.value));
  return { reconcile };
}
