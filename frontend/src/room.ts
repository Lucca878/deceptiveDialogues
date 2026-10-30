type Role = "interviewer" | "interviewee";

export {};

interface RoomResponse {
  code?: string;
  status?: "waiting" | "paired";
  role?: Role;
  error?: string;
}

function getElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required page element not found: ${selector}`);
  return element;
}

const retryButton = getElement<HTMLButtonElement>("#retry-matching");
const roomStatus = getElement<HTMLParagraphElement>("#room-status");

sessionStorage.removeItem("chat-role");
sessionStorage.removeItem("chat-room-code");
sessionStorage.removeItem("pending-room-code");

const storedParticipantId = sessionStorage.getItem("participant-id");
const participantId = storedParticipantId ?? crypto.randomUUID();
if (!storedParticipantId) sessionStorage.setItem("participant-id", participantId);

let pollingTimer: number | undefined;

function readResponse(response: Response): Promise<RoomResponse> {
  return response.json().then((data: RoomResponse) => {
    if (!response.ok) throw new Error(data.error ?? "Room request failed.");
    return data;
  });
}

function enterDialogue(code: string, role: Role): void {
  if (pollingTimer !== undefined) window.clearTimeout(pollingTimer);
  sessionStorage.removeItem("pending-room-code");
  sessionStorage.setItem("chat-room-code", code);
  sessionStorage.setItem("chat-role", role);
  window.location.assign("/dialogue");
}

async function checkRoomStatus(code: string): Promise<void> {
  try {
    const url = `/api/rooms/${encodeURIComponent(code)}?participant_id=${encodeURIComponent(participantId)}`;
    const response = await fetch(url);
    const data = await readResponse(response);

    if (data.status === "paired" && data.role) {
      enterDialogue(data.code ?? code, data.role);
      return;
    }

    pollingTimer = window.setTimeout(() => void checkRoomStatus(code), 1000);
  } catch (error) {
    roomStatus.textContent = error instanceof Error ? error.message : "Could not check the room status.";
    retryButton.hidden = false;
  }
}

function waitForRoom(code: string): void {
  roomStatus.textContent = "Waiting for another participant. Please keep this page open.";
  void checkRoomStatus(code);
}

async function findPartner(): Promise<void> {
  if (pollingTimer !== undefined) window.clearTimeout(pollingTimer);
  retryButton.hidden = true;
  roomStatus.textContent = "Finding another participant…";

  try {
    const response = await fetch("/api/rooms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId }),
    });
    const data = await readResponse(response);
    if (!data.code) throw new Error("The server did not return a room assignment.");
    if (data.status === "paired" && data.role) {
      enterDialogue(data.code, data.role);
    } else {
      waitForRoom(data.code);
    }
  } catch (error) {
    roomStatus.textContent = error instanceof Error ? error.message : "Could not find a participant.";
    retryButton.hidden = false;
  }
}

retryButton.addEventListener("click", () => void findPartner());
void findPartner();
