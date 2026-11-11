export {};

const page = document.querySelector<HTMLElement>("#role-instructions");
if (page?.dataset.ready === "true" && page.dataset.roomCode) {
  const code = page.dataset.roomCode;
  const status = document.querySelector<HTMLElement>("#readiness-status");
  async function checkReadiness(): Promise<void> {
    try {
      const response = await fetch(`/api/rooms/${encodeURIComponent(code)}`, { cache: "no-store" });
      const data = await response.json() as { chat_ready?: boolean; error?: string };
      if (!response.ok) {
        if (status) status.textContent = data.error ?? "Could not check the conversation status. Please refresh this page.";
        if (response.status < 500) return;
      } else if (data.chat_ready) {
        window.location.replace("/dialogue");
        return;
      } else if (status) {
        status.textContent = "Waiting for your partner. Please keep this page open.";
      }
    } catch {
      // Retry transient network failures without revealing partner activity.
    }
    window.setTimeout(() => void checkReadiness(), 1000);
  }
  void checkReadiness();
}