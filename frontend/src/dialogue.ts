export {};

function getElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`Required page element not found: ${selector}`);
  return element;
} // getElement helper function

const roleName = getElement<HTMLElement>("#role-name"); // Role name element in the chat header
const form = getElement<HTMLFormElement>("#chat-form"); // Chat form element
const input = getElement<HTMLInputElement>("#chat-input"); // Chat input field
const sendButton = getElement<HTMLButtonElement>(".chat-send"); // Send button
const messages = getElement<HTMLDivElement>("#chat-messages"); // Container for chat messages
const emptyState = getElement<HTMLParagraphElement>("#chat-empty"); // Empty state message element
const typingIndicator = getElement<HTMLParagraphElement>("#chat-typing"); // Typing indicator element 
const questionProgress = getElement<HTMLDivElement>("#question-progress"); // Question progress container
const questionCounter = getElement<HTMLParagraphElement>("#question-counter"); // Server-provided question progress status
const questionProgressNote = getElement<HTMLSpanElement>("#question-progress-note"); // Supporting question progress status
const questionProgressTrack = getElement<HTMLDivElement>("#question-progress-track"); // Accessible progress bar
const questionProgressFill = getElement<HTMLSpanElement>("#question-progress-fill"); // Visual progress bar fill
const role = sessionStorage.getItem("chat-role"); // User's role in the chat (interviewer or interviewee)
const transitionPanel = getElement<HTMLDivElement>("#transition-panel"); // Shared waiting/countdown panel
const transitionCountdown = getElement<HTMLDivElement>("#transition-countdown"); // Countdown shown after Continue
const judgementForm = 
  role === "interviewer"
  ? document.querySelector<HTMLFormElement>("#judgement-form") // Judgement form element (interviewer only)
  : null;
const judgementStatus = 
  role === "interviewer"
  ? getElement<HTMLParagraphElement>("#judgement-status") // Judgement status element (interviewer only)
  : null;
const continueButton = role === "interviewer"
  ? document.querySelector<HTMLButtonElement>("#judgement-continue")
  : null; // Only the interviewer page contains the Continue button.
const mazeBoard = role === "interviewee"
  ? document.querySelector<HTMLDivElement>("#maze-board")
  : null; // The optional maze is rendered only for the interviewee.
const mazeStatus = role === "interviewee"
  ? document.querySelector<HTMLParagraphElement>("#maze-status")
  : null; // Screen-reader status for maze movement.
const mazeControls = role === "interviewee"
  ? document.querySelectorAll<HTMLButtonElement>("[data-maze-direction]")
  : []; // Direction buttons are omitted from the interviewer page.
const roomCode = sessionStorage.getItem("chat-room-code"); // Active chat room code
const participantId = sessionStorage.getItem("participant-id"); // Unique identifier for the participant
if (
  (role !== "interviewer" && role !== "interviewee") ||
  !roomCode ||
  !participantId
) {
  window.location.replace("/room"); // Redirect to the room selection page if required session data is missing
} else {
  const activeRoomCode = roomCode!; // Non-null assertion for the active chat room code
  const activeParticipantId = participantId!; // Non-null assertion for the active participant ID
  roleName.textContent = role === "interviewer" ? "Interviewer" : "Interviewee"; // Set the role name in the chat header based on the user's role
  let lastMessageId = 0; // ID of the last message received from the server
  let lastTypingPing = 0; // Timestamp of the last typing ping sent to the server
  let openingClaimReceived = false; // Flag indicating whether the opening claim from the interviewee has been received
  // Track the server's can_send value and use it to enable/disable both composer controls.
  let canSend = false; // Flag indicating whether the server allows this participant to send messages
  const renderedMessageIds = new Set<number>(); // Set of message IDs that have already been rendered to avoid duplicates
  const mazeLayout = [ // Fixed, client-only maze layout; walls are 1 and walkable cells are 0.
    [0, 0, 1, 0, 0],
    [1, 0, 1, 0, 1],
    [0, 0, 0, 0, 0],
    [0, 1, 1, 1, 0],
    [0, 0, 0, 0, 0],
  ];
  let mazePosition = { row: 0, column: 0 }; // Start at the upper-left cell.
  type MazeDirection = "up" | "down" | "left" | "right";

  function renderMaze(): void {
    if (!mazeBoard) return;
    mazeBoard.replaceChildren();
    for (let row = 0; row < mazeLayout.length; row += 1) {
      for (let column = 0; column < mazeLayout[row].length; column += 1) {
        const cell = document.createElement("span");
        cell.className = "maze-cell";
        if (mazeLayout[row][column] === 1) cell.classList.add("maze-cell--wall");
        if (row === 0 && column === 0) cell.classList.add("maze-cell--start");
        if (row === mazeLayout.length - 1 && column === mazeLayout[row].length - 1) {
          cell.classList.add("maze-cell--exit");
        }
        if (row === mazePosition.row && column === mazePosition.column) {
          cell.classList.add("maze-cell--player");
        }
        cell.setAttribute("aria-hidden", "true"); // The live status and controls provide the accessible interaction.
        mazeBoard.append(cell);
      }
    }
  }

  function moveMaze(direction: MazeDirection): void {
    if (!mazeBoard || !mazeStatus) return;
    const offsets: Record<MazeDirection, [number, number]> = {
      up: [-1, 0],
      down: [1, 0],
      left: [0, -1],
      right: [0, 1],
    };
    const [rowOffset, columnOffset] = offsets[direction];
    const nextRow = mazePosition.row + rowOffset;
    const nextColumn = mazePosition.column + columnOffset;
    if (
      nextRow < 0 || nextRow >= mazeLayout.length ||
      nextColumn < 0 || nextColumn >= mazeLayout[nextRow].length ||
      mazeLayout[nextRow][nextColumn] === 1
    ) {
      mazeStatus.textContent = "That direction is blocked.";
      return;
    }

    mazePosition = { row: nextRow, column: nextColumn };
    renderMaze();
    if (nextRow === mazeLayout.length - 1 && nextColumn === mazeLayout[nextRow].length - 1) {
      mazeStatus.textContent = "Maze complete. You can keep waiting; the next dialogue will begin automatically.";
      return;
    }
    mazeStatus.textContent = `You are at row ${nextRow + 1}, column ${nextColumn + 1}.`;
  }

  if (mazeBoard && mazeStatus) {
    renderMaze();
    mazeBoard.addEventListener("keydown", (event: KeyboardEvent) => {
      const directionByKey: Record<string, MazeDirection> = {
        ArrowUp: "up",
        ArrowDown: "down",
        ArrowLeft: "left",
        ArrowRight: "right",
      };
      const direction = directionByKey[event.key];
      if (!direction) return;
      event.preventDefault();
      moveMaze(direction);
    });
    mazeControls.forEach((button) => {
      button.addEventListener("click", () => {
        const direction = button.dataset.mazeDirection;
        if (direction === "up" || direction === "down" || direction === "left" || direction === "right") {
          moveMaze(direction);
        }
      });
    });
  }

  function pingTyping(): void { // Sends a typing ping to the server to indicate that the participant is currently typing
    // Send typing pings only while the server says this participant may speak.
    if (!canSend) return; // Do not send typing pings if the server does not allow this participant to send messages
    if (!openingClaimReceived) return;  // Do not send typing pings until the opening claim has been received
    const now = Date.now(); // Current timestamp in milliseconds
    if (now - lastTypingPing < 2000) return; // Throttle typing pings to at most one every 2 seconds
    lastTypingPing = now; // Update the timestamp of the last typing ping

    void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/typing`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: activeParticipantId }),
    }).catch(() => {
      // Typing pings are best-effort only. Failures are silently ignored.
    });
  }

  function renderMessage(message: { // Renders a single chat message in the UI
    id: number;
    participant_id: string;
    role: string;
    text: string;
    is_claim?: boolean;
  }): void {
    if (renderedMessageIds.has(message.id)) return; // Skip rendering if this message has already been rendered
    renderedMessageIds.add(message.id); // Mark this message ID as rendered to prevent duplicate rendering
    emptyState.remove(); // Remove the empty state message when a new message is rendered

    const bubble = document.createElement("p"); // Create a new paragraph element for the chat message
    bubble.className = "chat-message"; // Apply the base chat message styling
    if (message.participant_id !== activeParticipantId) {
      bubble.classList.add("chat-message--remote"); // Style the message as a remote message if it's from the other participant
    }
    bubble.textContent = message.text; // Set the text content of the chat message bubble
    messages.append(bubble); // Append the chat message bubble to the messages container
    if (message.is_claim === true) {
      openingClaimReceived = true;
      input.placeholder = "Write a message...";
    }
    messages.scrollTop = messages.scrollHeight; // Scroll to the bottom of the messages container to show the latest message
  }

  async function pollMessages(): Promise<void> { // Polls for new messages and updates the chat UI
    try {
      const query = new URLSearchParams({
        participant_id: activeParticipantId, // Include the participant ID in the query parameters
        after_id: String(lastMessageId), // Only fetch messages after the last known message ID
      });
      const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages?${query}`); // Fetch new messages from the server
      const data = await response.json() as {
        messages?: Array<{
          id: number;
          participant_id: string;
          role: string;
          text: string;
          is_claim?: boolean;
        }>; // Array of new messages from the server
        partner_typing?: boolean;
        can_send?: boolean;
        question_number?: number;
        question_turns_ended?: boolean;
        dialogue_complete?: boolean;
        transition_complete?: boolean;
        error?: string;
        seconds_remaining?: number;
      };
      if (!response.ok) throw new Error(data.error ?? "Could not load messages."); // Throw an error if the response is not OK
      const countdownActive = typeof data.seconds_remaining === "number";
      const intervieweeWaiting = role === "interviewee" && data.question_turns_ended === true;
      const showTransitionPanel = countdownActive || intervieweeWaiting;
      transitionPanel.hidden = !showTransitionPanel;
      messages.hidden = role === "interviewee" && showTransitionPanel;
      transitionCountdown.hidden = !countdownActive;
      if (countdownActive && typeof data.seconds_remaining === "number") {
        const secondsRemaining = Math.ceil(data.seconds_remaining);
        transitionCountdown.textContent = String(secondsRemaining);
        if (judgementForm) judgementForm.hidden = true;
        if (continueButton) continueButton.hidden = true;
      }
      if (data.dialogue_complete === true) {
        window.location.replace("/feedback"); // Open the guarded feedback URL when the server reports completion.
        return;
      }
      if (data.transition_complete === true) {
        window.location.replace("/dialogue"); // Navigate to the next dialogue after this participant's transition signal.
        return;
      }
      canSend = data.can_send === true;  // Update the canSend flag based on the server's response
      if (Number.isInteger(data.question_number) && data.question_number! >= 1 && data.question_number! <= 10) {
        const questionNumber = data.question_number!;
        questionProgress.hidden = false;
        questionProgressTrack.setAttribute("aria-valuenow", String(questionNumber));
        questionProgressFill.style.width = `${questionNumber * 10}%`;
        if (data.question_turns_ended) {
          if (judgementForm) {
            judgementForm.hidden = false;
          }
          questionCounter.textContent = "Questions complete";
          questionProgressNote.textContent = "Please wait for the next step.";
          questionProgressTrack.setAttribute("aria-label", "All 10 questions completed");
        } else {
          questionCounter.textContent = `Question ${questionNumber} of 10`;
          questionProgressNote.textContent = "";
          questionProgressTrack.setAttribute("aria-label", "Questions completed");
        }
      }
      typingIndicator.hidden = !data.partner_typing; // Show or hide the typing indicator based on the partner's typing status

      for (const message of data.messages ?? []) {
        lastMessageId = Math.max(lastMessageId, message.id);
        renderMessage(message); // Render the new message in the chat UI
      }
      input.disabled = !canSend || !openingClaimReceived;
      sendButton.disabled = !canSend || !openingClaimReceived;
    } catch (error) {
      console.error(error instanceof Error ? error.message : "Could not load messages."); // Log any errors encountered during message polling
    } finally {
      window.setTimeout(() => void pollMessages(), 1000); // Schedule the next poll after 1 second
    }
  }

  void pollMessages(); // Start polling for new messages immediately

  input.addEventListener("input", pingTyping);

  // Add a submit handler for the optional judgement form; prevent native navigation and POST its truth/confidence values as JSON to the judgement endpoint.
  async function submitJudgement(event: Event) {
    event.preventDefault(); // Prevent the default form submission behavior
    if (judgementForm) { // Check if the judgement form exists
      const formData = new FormData(judgementForm);
      const truth = formData.get("truth");
      const confidence = formData.get("confidence");
      if (!truth || !confidence) return;
      try {
        const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/judgement`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ participant_id: activeParticipantId, judgement: truth, confidence: Number(confidence) }),
        });
        if (!response.ok) throw new Error("Could not submit judgement.");
        const result = await response.json() as { correct?: boolean };
        if (judgementStatus) {
          if (typeof result.correct === "boolean") {
            judgementStatus.textContent = result.correct ? "Correct" : "Incorrect";
            judgementStatus.hidden = false;
            if (continueButton) {
              continueButton.hidden = false;
            }
          } else {
            judgementStatus.textContent = "";
            judgementStatus.hidden = true;
          }
        }
      } catch (error) {
        console.error(error instanceof Error ? error.message : "Could not submit judgement.");
      }
    }
  }
  if (judgementForm) {
    judgementForm.addEventListener("submit", submitJudgement);
  }

  async function continueToNextClaim() {
    if (!continueButton) return;
    continueButton.disabled = true; // Prevent duplicate Continue requests while this one is processing.
    try {
      const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/continue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ participant_id: activeParticipantId }),
      }); // Ask the server to advance the room after this judgement.
      if (!response.ok) throw new Error("Could not continue to the next claim.");
      // Keep both participants on this page so polling can show the shared countdown.
    } catch (error) {
      continueButton.disabled = false; // Allow a retry if the server could not advance the room.
      console.error(error instanceof Error ? error.message : "Could not continue to the next claim.");
    }
  }
  if (continueButton) {
    continueButton.addEventListener("click", continueToNextClaim);
  }
  
  form.addEventListener("submit", (event) => {
    event.preventDefault(); // Prevent the default form submission behavior

    if (!openingClaimReceived) return;
    const text = input.value.trim(); // Get the trimmed text from the input field
    if (!text) return; // Do nothing if the input is empty
    input.value = ""; // Clear the input field
    input.disabled = true; // Disable the input field while sending the message
    sendButton.disabled = true;

    void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages`, { // Send a new message to the server
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ participant_id: activeParticipantId, text }),
    }) // Handle the server response for the new message
      .then(async (response) => { // Process the server response for the new message
        const data = await response.json() as {
          message?: {
            id: number;
            participant_id: string;
            role: string;
            text: string;
            is_claim?: boolean;
          };
          error?: string;
        };
        if (!response.ok || !data.message) throw new Error(data.error ?? "Could not send message."); // Throw an error if the message could not be sent
        canSend = false; // The server has switched the turn; wait for polling to confirm the next permission
        renderMessage(data.message); // Render the newly sent message in the chat UI
      })
      .catch((error: unknown) => {
        input.value = text;
        console.error(error instanceof Error ? error.message : "Could not send message."); // Log any errors encountered while sending the message
      })
      .finally(() => {
        // Keep the composer disabled after sending until polling says this participant's turn is active again.
        input.disabled = !canSend || !openingClaimReceived;
        sendButton.disabled = !canSend || !openingClaimReceived;
        if (openingClaimReceived) input.focus(); // Focus the input field after sending a message
      });
  }); // Handle the form submission for sending a new message
}


// polling means repeatedly checking the server for new messages at regular intervals
// In this code, pollMessages is called every second to fetch new messages from the server
// and update the chat UI accordingly
// server pushed messages would be received in real-time without the need for polling and would update the chat UI immediately; server opens a connection to the client and sends new messages as they arrive