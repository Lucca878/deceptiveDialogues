type Role = "interviewer" | "interviewee"; // Represents the role of a participant in the room

export {}; // Ensures this file is treated as a module

interface RoomResponse { // Represents the response from the server when checking or joining a room
  code?: string;
  status?: "waiting" | "paired";
  role?: Role;
  error?: string;
}

class RoomRequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "RoomRequestError";
  }
}

function getElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required page element not found: ${selector}`);
  return element;
} // Retrieves a required page element by its selector, throwing an error if not found

const retryButton = getElement<HTMLButtonElement>("#retry-matching"); // The button to retry finding a partner
const roomStatus = getElement<HTMLParagraphElement>("#room-status"); // The paragraph element displaying the room status
const matchingProgress = getElement<HTMLDivElement>("#matching-progress");
const matchingRing = getElement<HTMLDivElement>("#matching-ring");
const motionToggle = getElement<HTMLButtonElement>("#matching-motion-toggle");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

let matchingStage: "waiting" | "paired" | "error" = "waiting";
let animationPaused = false;

function updateMatchingDisplay(): void {
  const waiting = matchingStage === "waiting";
  matchingRing.hidden = !waiting;
  matchingRing.classList.toggle("matching-ring--animated", waiting && !animationPaused && !reducedMotion.matches);
  motionToggle.hidden = !waiting || reducedMotion.matches;
  motionToggle.disabled = reducedMotion.matches;
  motionToggle.setAttribute("aria-pressed", String(animationPaused));
  motionToggle.textContent = animationPaused ? "Resume animation" : "Pause animation";
}

motionToggle.addEventListener("click", () => {
  animationPaused = !animationPaused;
  updateMatchingDisplay();
});
reducedMotion.addEventListener("change", updateMatchingDisplay);

sessionStorage.removeItem("chat-role"); // Clears the stored chat role
sessionStorage.removeItem("chat-room-code"); // Clears the stored chat room code
sessionStorage.removeItem("pending-room-code"); // Clears the stored pending room code

const storedParticipantId = sessionStorage.getItem("participant-id"); // Retrieves the stored participant ID, if any
const participantId = storedParticipantId ?? crypto.randomUUID(); // Generates a new participant ID if none is stored
if (!storedParticipantId) sessionStorage.setItem("participant-id", participantId); // Stores the new participant ID if it was generated 

let pollingTimer: number | undefined; // Stores the ID of the polling timer for checking room status

function readResponse(response: Response): // Reads and parses the JSON response from the server, throwing an error if the response is not OK
Promise<RoomResponse> {
  return response.json().then((data: RoomResponse) => {
    if (!response.ok) throw new RoomRequestError(data.error ?? "Room request failed.", response.status);
    return data;
  });
} // Reads and parses the JSON response from the server, throwing an error if the response is not OK

function showRequestError(error: unknown, fallback: string): void {
  matchingStage = "error";
  updateMatchingDisplay();
  roomStatus.textContent = error instanceof Error ? error.message : fallback;
  retryButton.hidden = error instanceof RoomRequestError && error.status >= 400 && error.status < 500;
}

function enterDialogue(code: string, role: Role): void {
  if (pollingTimer !== undefined) window.clearTimeout(pollingTimer);
  sessionStorage.removeItem("pending-room-code");
  sessionStorage.setItem("chat-room-code", code);
  sessionStorage.setItem("chat-role", role);
  matchingStage = "paired";
  updateMatchingDisplay();
  window.location.assign("/role-instructions");
} // Handles entering the dialogue page with the assigned room code and role

async function checkRoomStatus(code: string): Promise<void> { // Checks the status of a specific room for the participant
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
    showRequestError(error, "Could not check the room status.");
  }
} // Checks the status of a specific room for the participant

function waitForRoom(code: string): void { // Displays a waiting message and starts polling for room status
  roomStatus.textContent = "Waiting to be paired with another participant...";
  void checkRoomStatus(code);
}

async function findPartner(): Promise<void> { // Initiates the process of finding a partner for the participant
  if (pollingTimer !== undefined) window.clearTimeout(pollingTimer); // Clears any existing polling timer before starting a new partner search
  matchingStage = "waiting";
  updateMatchingDisplay();
  matchingProgress.hidden = false;
  roomStatus.hidden = false;
  retryButton.hidden = true; // Hides the retry button while searching for a partner
  roomStatus.textContent = "Waiting to be paired with another participant..."; // Sets the initial waiting message for the participant

  try {
    const response = await fetch("/api/rooms", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: participantId }),
    }); // Sends a request to create or join a room for the participant
    const data = await readResponse(response); // Reads and parses the JSON response from the server
    if (!data.code) throw new Error("The server did not return a room assignment."); // Throws an error if the server response does not include a room code
    if (data.status === "paired" && data.role) {
      enterDialogue(data.code, data.role);
    } else {
      waitForRoom(data.code);
    } // Handles the case where the room is not yet paired and the participant needs to wait for a partner
  } catch (error) {
    showRequestError(error, "Could not find a participant.");
  } // Handles errors that occur while trying to find a partner
} // Initiates the process of finding a partner for the participant

retryButton.addEventListener("click", () => void findPartner());
void findPartner(); // Starts the process of finding a partner when the page loads
