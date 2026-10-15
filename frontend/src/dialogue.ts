export {};

function getElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required page element not found: ${selector}`);
  return element;
}

const roleName = getElement<HTMLElement>("#role-name");
const form = getElement<HTMLFormElement>("#chat-form");
const input = getElement<HTMLInputElement>("#chat-input");
const messages = getElement<HTMLDivElement>("#chat-messages");
const emptyState = getElement<HTMLParagraphElement>("#chat-empty");
const typingIndicator = getElement<HTMLParagraphElement>("#chat-typing");

const role = sessionStorage.getItem("chat-role");
const roomCode = sessionStorage.getItem("chat-room-code");
const participantId = sessionStorage.getItem("participant-id");
if (
  (role !== "interviewer" && role !== "interviewee") ||
  !roomCode ||
  !participantId
) {
  window.location.replace("/room");
} else {
  const activeRoomCode = roomCode!;
  const activeParticipantId = participantId!;
  roleName.textContent = role === "interviewer" ? "Interviewer" : "Interviewee";
  let lastMessageId = 0;
  let lastTypingPing = 0;
  const renderedMessageIds = new Set<number>();

  function pingTyping(): void {
    const now = Date.now();
    if (now - lastTypingPing < 2000) return;
    lastTypingPing = now;

    void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/typing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: activeParticipantId }),
    }).catch(() => {
      // Typing pings are best-effort only.
    });
  }

  function renderMessage(message: {
    id: number;
    participant_id: string;
    text: string;
  }): void {
    if (renderedMessageIds.has(message.id)) return;
    renderedMessageIds.add(message.id);
    emptyState.remove();

    const bubble = document.createElement("p");
    bubble.className = "chat-message";
    if (message.participant_id !== activeParticipantId) {
      bubble.classList.add("chat-message--remote");
    }
    bubble.textContent = message.text;
    messages.append(bubble);
    messages.scrollTop = messages.scrollHeight;
  }

  async function pollMessages(): Promise<void> {
    try {
      const query = new URLSearchParams({
        participant_id: activeParticipantId,
        after_id: String(lastMessageId),
      });
      const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages?${query}`);
      const data = await response.json() as {
        messages?: Array<{
          id: number;
          participant_id: string;
          text: string;
        }>;
        partner_typing?: boolean;
        error?: string;
      };
      if (!response.ok) throw new Error(data.error ?? "Could not load messages.");

      typingIndicator.hidden = !data.partner_typing;

      for (const message of data.messages ?? []) {
        lastMessageId = Math.max(lastMessageId, message.id);
        renderMessage(message);
      }
    } catch (error) {
      console.error(error instanceof Error ? error.message : "Could not load messages.");
    } finally {
      window.setTimeout(() => void pollMessages(), 1000);
    }
  }

  void pollMessages();

  input.addEventListener("input", pingTyping);

  form.addEventListener("submit", (event) => {
    event.preventDefault();

    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.disabled = true;

    void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: activeParticipantId, text }),
    })
      .then(async (response) => {
        const data = await response.json() as {
          message?: {
            id: number;
            participant_id: string;
            text: string;
          };
          error?: string;
        };
        if (!response.ok || !data.message) throw new Error(data.error ?? "Could not send message.");
        renderMessage(data.message);
      })
      .catch((error: unknown) => {
        input.value = text;
        console.error(error instanceof Error ? error.message : "Could not send message.");
      })
      .finally(() => {
        input.disabled = false;
        input.focus();
      });
  });
}
