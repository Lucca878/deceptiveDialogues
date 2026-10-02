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

const createButton = getElement<HTMLButtonElement>("#create-room");
const joinForm = getElement<HTMLFormElement>("#join-room-form");
const joinButton = getElement<HTMLButtonElement>("#join-room");
const roomCodeInput = getElement<HTMLInputElement>("#room-code-input");
const roomCodeDisplay = getElement<HTMLDivElement>("#room-code-display");
const roomCode = getElement<HTMLElement>("#room-code");
const roomStatus = getElement<HTMLParagraphElement>("#room-status");

sessionStorage.removeItem("chat-role");
sessionStorage.removeItem("chat-room-code");

const storedParticipantId = sessionStorage.getItem("participant-id");
const participantId = storedParticipantId ?? crypto.randomUUID();
if (!storedParticipantId) sessionStorage.setItem("participant-id", participantId);

let pollingTimer: number | undefined;

function setLobbyBusy(isBusy: boolean): void {
  createButton.disabled = isBusy;
  joinButton.disabled = isBusy;
  roomCodeInput.disabled = isBusy;
}

function showRoomCode(code: string): void {
  roomCode.textContent = code;
  roomCodeDisplay.hidden = false;
}

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
    sessionStorage.removeItem("pending-room-code");
    roomStatus.textContent = error instanceof Error ? error.message : "Could not check the room status.";
    setLobbyBusy(false);
  }
}

function waitForRoom(code: string): void {
  sessionStorage.setItem("pending-room-code", code);
  showRoomCode(code);
  roomStatus.textContent = "Waiting for the other participant to join...";
  setLobbyBusy(true);
  void checkRoomStatus(code);
}

createButton.addEventListener("click", async () => {
  setLobbyBusy(true);
  roomStatus.textContent = "Creating your room...";

  try {
    const response = await fetch("/api/rooms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId }),
    });
    const data = await readResponse(response);
    if (!data.code) throw new Error("The server did not return a room code.");
    waitForRoom(data.code);
  } catch (error) {
    roomStatus.textContent = error instanceof Error ? error.message : "Could not create a room.";
    setLobbyBusy(false);
  }
});

joinForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = roomCodeInput.value.trim().toUpperCase();
  if (!code) return;

  setLobbyBusy(true);
  roomStatus.textContent = "Joining room...";

  try {
    const response = await fetch(`/api/rooms/${encodeURIComponent(code)}/join`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId }),
    });
    const data = await readResponse(response);
    const joinedCode = data.code ?? code;
    showRoomCode(joinedCode);

    if (data.status === "paired" && data.role) {
      enterDialogue(joinedCode, data.role);
    } else {
      waitForRoom(joinedCode);
    }
  } catch (error) {
    roomStatus.textContent = error instanceof Error ? error.message : "Could not join the room.";
    setLobbyBusy(false);
  }
});

const pendingCode = sessionStorage.getItem("pending-room-code");
if (pendingCode) waitForRoom(pendingCode);
