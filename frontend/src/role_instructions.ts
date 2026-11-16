export {};

const page = document.querySelector<HTMLElement>("#role-instructions");
if (page?.dataset.ready === "true" && page.dataset.roomCode) {
  const code = page.dataset.roomCode;
  const status = document.querySelector<HTMLElement>("#readiness-status");
  const ring = document.querySelector<HTMLElement>("#readiness-ring");
  const motionToggle = document.querySelector<HTMLButtonElement>("#readiness-motion-toggle");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let animationPaused = false;

  // Keep the decorative ring animation and control in sync with user preferences.
  function updateWaitingAnimation(): void {
    if (!ring || !motionToggle) return;
    ring.classList.toggle("matching-ring--animated", !animationPaused && !reducedMotion.matches);
    motionToggle.hidden = reducedMotion.matches;
    motionToggle.disabled = reducedMotion.matches;
    motionToggle.setAttribute("aria-pressed", String(animationPaused));
    motionToggle.textContent = animationPaused ? "Resume animation" : "Pause animation";
  }

  // Let participants pause or resume the ring without affecting room matching.
  motionToggle?.addEventListener("click", () => {
    animationPaused = !animationPaused;
    updateWaitingAnimation();
  });
  reducedMotion.addEventListener("change", updateWaitingAnimation);
  updateWaitingAnimation();

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
        status.textContent = "Please keep this page open while you are waiting.";
      }
    } catch {
      // Retry transient network failures without revealing partner activity.
    }
    window.setTimeout(() => void checkReadiness(), 1000);
  }
  void checkReadiness();
}