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
const dialogueCounter = getElement<HTMLParagraphElement>("#dialogue-counter"); // Current server-reported dialogue label
const dialogueProgressTrack = getElement<HTMLDivElement>("#dialogue-progress-track"); // Accessible dialogue progress bar
const dialogueProgressFill = getElement<HTMLSpanElement>("#dialogue-progress-fill"); // Visual dialogue progress fill
const questionCounter = getElement<HTMLParagraphElement>("#question-counter"); // Server-provided question progress status
const questionProgressNote = getElement<HTMLSpanElement>("#question-progress-note"); // Supporting question progress status
const questionProgressTrack = getElement<HTMLDivElement>("#question-progress-track"); // Accessible progress bar
const questionProgressFill = getElement<HTMLSpanElement>("#question-progress-fill"); // Visual progress bar fill
const role = sessionStorage.getItem("chat-role"); // User's role in the chat (interviewer or interviewee)
const transitionPanel = getElement<HTMLDivElement>("#transition-panel"); // Shared waiting/countdown panel
const transitionMessage = getElement<HTMLParagraphElement>("#transition-message"); // Neutral status before the shared countdown
const transitionCountdownLabel = getElement<HTMLParagraphElement>("#transition-countdown-label"); // Shared countdown heading
const transitionCountdown = getElement<HTMLDivElement>("#transition-countdown"); // Countdown shown after Continue
const transitionMaze = document.querySelector<HTMLElement>("#transition-maze"); // Optional interviewee-only activity
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
const judgementFields = role === "interviewer"
  ? document.querySelector<HTMLFieldSetElement>("#judgement-fields")
  : null; // Fieldset containing the interviewer judgement controls.
const judgementTruthStep = role === "interviewer"
  ? document.querySelector<HTMLElement>("#judgement-truth-step")
  : null; // Truth-only step of the judgement form.
const judgementConfidenceStep = role === "interviewer"
  ? document.querySelector<HTMLElement>("#judgement-confidence-step")
  : null; // Confidence-only step of the judgement form.
const judgementTruthSubmit = role === "interviewer"
  ? document.querySelector<HTMLButtonElement>("#judgement-truth-submit")
  : null; // Advances from the truth choice to confidence without submitting partial data.
const judgementConfidenceSubmit = role === "interviewer"
  ? document.querySelector<HTMLButtonElement>("#judgement-confidence-submit")
  : null; // Submits confidence only after the slider has been interacted with.
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
const roomParticipantId = sessionStorage.getItem("room-participant-id"); // Temporary identifier bound to this room membership.
const questionTypeStep = document.querySelector<HTMLDivElement>("#question-type-step"); // Second composer stage shown after Send is selected
const questionTypeSelect = document.querySelector<HTMLSelectElement>("#question-type"); // Interviewer-only question category selector
if (
  (role !== "interviewer" && role !== "interviewee") ||
  !roomCode ||
  !roomParticipantId
) {
  window.location.replace("/room"); // Redirect to the room selection page if required session data is missing
} else {
  const activeRoomCode = roomCode!; // Non-null assertion for the active chat room code
  const activeRoomParticipantId = roomParticipantId!; // Non-null assertion for the active participant ID
  roleName.textContent = role === "interviewer" ? "Interviewer" : "Interviewee"; // Set the role name in the chat header based on the user's role
  let lastMessageId = 0; // ID of the last message received from the server
  let lastTypingPing = 0; // Timestamp of the last typing ping sent to the server
  let openingClaimReceived = false; // Flag indicating whether the opening claim from the interviewee has been received
  // Track the server's can_send value and use it to enable/disable both composer controls.
  let canSend = false; // Flag indicating whether the server allows this participant to send messages
  let selectingQuestionType = false; // Tracks whether the sent interviewer question still needs a category.
  let messageRequestPending = false; // Prevents duplicate message submissions while the server is processing one.
  const renderedMessageIds = new Set<number>(); // Set of message IDs that have already been rendered to avoid duplicates
  const mazeSize = 9; // Increase the optional client-only maze to make routes less predictable.
  const minimumMazePathLength = 24; // Reject layouts with a short, obvious route to the exit.
  let mazeLayout: number[][] = []; // Store only the current maze locally; never send maze state to the server.
  let previousMazeKey = ""; // Avoid immediately showing the same generated layout twice.
  let mazePosition = { row: 0, column: 0 }; // Start at the upper-left cell.
  type MazeDirection = "up" | "down" | "left" | "right";

  function shortestMazePathLength(layout: number[][]): number | null {
    const queue: Array<[number, number, number]> = [[0, 0, 0]]; // Breadth-first search records the shortest number of moves from the start.
    const visited = new Set(["0,0"]);
    const steps: Array<[number, number]> = [[-1, 0], [1, 0], [0, -1], [0, 1]];
    while (queue.length > 0) {
      const [row, column, distance] = queue.shift()!;
      if (row === mazeSize - 1 && column === mazeSize - 1) return distance;
      for (const [rowStep, columnStep] of steps) {
        const nextRow = row + rowStep;
        const nextColumn = column + columnStep;
        const key = `${nextRow},${nextColumn}`;
        if (
          nextRow >= 0 && nextRow < mazeSize &&
          nextColumn >= 0 && nextColumn < mazeSize &&
          layout[nextRow][nextColumn] === 0 &&
          !visited.has(key)
        ) {
          visited.add(key);
          queue.push([nextRow, nextColumn, distance + 1]);
        }
      }
    }
    return null; // Return null when walls completely block the exit.
  }

  function createMaze(): number[][] {
    for (let attempt = 0; attempt < 5000; attempt += 1) { // Longest-route layouts are rare, so retry before using the guaranteed fallback.
      const candidate = Array.from({ length: mazeSize }, () =>
        Array.from({ length: mazeSize }, () => Math.random() < 0.34 ? 1 : 0),
      ); // Increase wall density to create more branches and dead ends.
      candidate[0][0] = 0; // Keep the start open.
      candidate[mazeSize - 1][mazeSize - 1] = 0; // Keep the exit open.
      const key = candidate.map((row) => row.join("")).join("/");
      const shortestPathLength = shortestMazePathLength(candidate);
      if (
        key !== previousMazeKey &&
        shortestPathLength !== null &&
        shortestPathLength >= minimumMazePathLength
      ) {
        previousMazeKey = key;
        return candidate;
      }
    }
    // Carve a guaranteed solvable serpentine route instead of falling back to an easy open grid.
    const fallback = Array.from({ length: mazeSize }, () => Array.from({ length: mazeSize }, () => 1));
    for (let row = 0; row < mazeSize; row += 2) fallback[row].fill(0); // Open alternating rows as long corridors.
    for (let row = 1; row < mazeSize; row += 2) {
      const connectorColumn = ((row - 1) / 2) % 2 === 0 ? mazeSize - 1 : 0; // Alternate the corridor connector side.
      fallback[row][connectorColumn] = 0;
    }
    previousMazeKey = fallback.map((row) => row.join("")).join("/"); // Remember the fallback so random generation can reject it next time.
    return fallback;
  }

  function startFreshMaze(statusText: string): void {
    mazeLayout = createMaze();
    mazePosition = { row: 0, column: 0 };
    renderMaze();
    if (mazeStatus) mazeStatus.textContent = statusText;
  }

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
      startFreshMaze("Maze complete. A new maze is ready if you'd like to keep exploring.");
      return;
    }
    mazeStatus.textContent = `You are at row ${nextRow + 1}, column ${nextColumn + 1}.`;
  }

  if (mazeBoard && mazeStatus) {
    startFreshMaze(`Maze start: row 1, column 1. Exit: row ${mazeSize}, column ${mazeSize}.`);
    transitionPanel.addEventListener("keydown", (event: KeyboardEvent) => {
      if (transitionPanel.hidden) return;
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
      body: JSON.stringify({ room_participant_id: activeRoomParticipantId }),
    }).catch(() => {
      // Typing pings are best-effort only. Failures are silently ignored.
    });
  }

  function renderMessage(message: { // Renders a single chat message in the UI
    id: number;
    room_participant_id: string;
    role: string;
    text: string;
    is_claim?: boolean;
  }): void {
    if (renderedMessageIds.has(message.id)) return; // Skip rendering if this message has already been rendered
    renderedMessageIds.add(message.id); // Mark this message ID as rendered to prevent duplicate rendering
    emptyState.remove(); // Remove the empty state message when a new message is rendered

    const bubble = document.createElement("p"); // Create a new paragraph element for the chat message
    bubble.className = "chat-message"; // Apply the base chat message styling
    if (message.room_participant_id !== activeRoomParticipantId) {
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
        room_participant_id: activeRoomParticipantId, // Identify the bound room membership to the server.
        after_id: String(lastMessageId), // Only fetch messages after the last known message ID
      });
      const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages?${query}`); // Fetch new messages from the server
      const data = await response.json() as {
        messages?: Array<{
          id: number;
          room_participant_id: string;
          role: string;
          text: string;
          is_claim?: boolean;
        }>; // Array of new messages from the server
        partner_typing?: boolean;
        can_send?: boolean;
        question_type_pending?: boolean;
        question_number?: number;
        question_turns_ended?: boolean;
        dialogue_complete?: boolean;
        dialogue_number?: number | null; // Active dialogue number, or null when no claim is active
        transition_complete?: boolean;
        error?: string;
        seconds_remaining?: number;
      };
      if (!response.ok) throw new Error(data.error ?? "Could not load messages."); // Throw an error if the response is not OK
      const countdownActive = typeof data.seconds_remaining === "number";
      const questionTypePending = role === "interviewer" && data.question_type_pending === true;
      const intervieweeWaiting = role === "interviewee" && data.question_turns_ended === true;
      const showTransitionPanel = countdownActive || intervieweeWaiting;
      transitionPanel.hidden = !showTransitionPanel;
      transitionPanel.classList.toggle("transition-panel--countdown", countdownActive);
      messages.hidden = countdownActive || intervieweeWaiting;
      questionProgress.hidden = countdownActive;
      transitionMessage.hidden = countdownActive;
      transitionCountdownLabel.hidden = !countdownActive;
      transitionCountdown.hidden = !countdownActive;
      if (transitionMaze) transitionMaze.hidden = countdownActive;
      if (countdownActive) {
        typingIndicator.hidden = true;
        form.hidden = true;
        if (judgementForm) judgementForm.hidden = true;
        if (continueButton) continueButton.hidden = true;
      }
      if (countdownActive && typeof data.seconds_remaining === "number") {
        const secondsRemaining = Math.ceil(data.seconds_remaining);
        transitionCountdown.textContent = String(secondsRemaining);
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
      if (questionTypePending && !selectingQuestionType) {
        // Restore the category step if the interviewer reloads while a question is awaiting classification.
        setQuestionTypeStep(true);
      } else if (!questionTypePending && selectingQuestionType && !messageRequestPending) {
        // Hide the category step once the server confirms the gate has been cleared.
        setQuestionTypeStep(false);
      }
      if (Number.isInteger(data.dialogue_number) && data.dialogue_number! >= 1 && data.dialogue_number! <= 10) {
        const dialogueNumber = data.dialogue_number!;
        dialogueCounter.textContent = `Dialogue ${dialogueNumber} of 10`;
        dialogueProgressTrack.setAttribute("aria-valuenow", String(dialogueNumber));
        dialogueProgressFill.style.width = `${dialogueNumber * 10}%`;
      } else {
        dialogueCounter.textContent = "";
        dialogueProgressTrack.setAttribute("aria-valuenow", "0");
        dialogueProgressFill.style.width = "0%";
      }
      if (Number.isInteger(data.question_number) && data.question_number! >= 1 && data.question_number! <= 10) {
        const questionNumber = data.question_number!;
        questionProgress.hidden = false;
        questionProgressTrack.setAttribute("aria-valuenow", String(questionNumber));
        questionProgressFill.style.width = `${questionNumber * 10}%`;
        if (data.question_turns_ended) {
          // Keep the category selector visible if the final answer arrives before its question is categorized.
          form.hidden = role !== "interviewer" || !questionTypePending;
          if (judgementForm) {
            judgementForm.hidden = countdownActive || questionTypePending;
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
      typingIndicator.hidden = countdownActive || !data.partner_typing; // Keep typing activity hidden while the shared transition screen is shown

      for (const message of data.messages ?? []) {
        lastMessageId = Math.max(lastMessageId, message.id);
        renderMessage(message); // Render the new message in the chat UI
      }
      input.disabled = selectingQuestionType || !canSend || !openingClaimReceived;
      sendButton.disabled = selectingQuestionType
        ? messageRequestPending || !questionTypePending
        : messageRequestPending || !canSend || !openingClaimReceived;
      if (questionTypeSelect) {
        questionTypeSelect.disabled = !selectingQuestionType || messageRequestPending || !questionTypePending;
      }
    } catch (error) {
      console.error(error instanceof Error ? error.message : "Could not load messages."); // Log any errors encountered during message polling
    } finally {
      window.setTimeout(() => void pollMessages(), 1000); // Schedule the next poll after 1 second
    }
  }

  void pollMessages(); // Start polling for new messages immediately

  input.addEventListener("input", pingTyping);

  function setQuestionTypeStep(show: boolean): void {
    // Keep the text composer and category selector mutually exclusive for interviewer turns.
    if (!questionTypeSelect || !questionTypeStep) return;
    selectingQuestionType = show;
    input.hidden = show;
    input.disabled = show || !canSend || !openingClaimReceived;
    questionTypeStep.hidden = !show;
    questionTypeSelect.disabled = !show;
    sendButton.textContent = show ? "Submit question type" : "Send";
  }

  const confidenceInput = document.getElementById("judgement-confidence") as HTMLInputElement | null;
  const confidenceOutput = document.getElementById("confidence-output") as HTMLOutputElement | null;
  let confidenceSliderTouched = false; // Require an explicit slider interaction even when its initial value is valid.
  if (confidenceInput && confidenceOutput) {
    confidenceInput.addEventListener("input", () => {
      confidenceOutput.value = confidenceInput.value;
      confidenceSliderTouched = true;
      if (judgementConfidenceSubmit) judgementConfidenceSubmit.disabled = false;
    });
    // Pointer/touch interaction counts even when the participant selects the slider's existing value.
    confidenceInput.addEventListener("pointerdown", () => {
      confidenceSliderTouched = true;
      if (judgementConfidenceSubmit) judgementConfidenceSubmit.disabled = false;
    });
    // Allow keyboard users to unlock Submit by operating the range control too.
    confidenceInput.addEventListener("keydown", (event: KeyboardEvent) => {
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
        confidenceSliderTouched = true;
        if (judgementConfidenceSubmit) judgementConfidenceSubmit.disabled = false;
      }
    });
  }
  // Enable truth submission only after one of the required radio choices is selected.
  judgementForm?.querySelectorAll<HTMLInputElement>('input[name="truth"]').forEach((choice) => {
    choice.addEventListener("change", () => {
      if (judgementTruthSubmit) judgementTruthSubmit.disabled = false;
    });
  });
  // Show the confidence step only after a truth choice has been made.
  function advanceToConfidence(): void {
    if (!judgementForm || !judgementTruthStep || !judgementConfidenceStep) return;
    const selectedTruth = judgementForm.querySelector<HTMLInputElement>('input[name="truth"]:checked');
    if (!selectedTruth) {
      judgementForm.querySelector<HTMLInputElement>('input[name="truth"]')?.reportValidity();
      return;
    }
    judgementTruthStep.hidden = true;
    judgementConfidenceStep.hidden = false;
    confidenceInput?.focus();
  }
  judgementTruthSubmit?.addEventListener("click", advanceToConfidence);

  const judgementFeedback = role === "interviewer" 
    ? document.getElementById("judgement-feedback") as HTMLDivElement | null
    : null;
  // Add a submit handler for the optional judgement form; prevent native navigation and POST its truth/confidence values as JSON to the judgement endpoint.
  async function submitJudgement(event: Event) {
    event.preventDefault(); // Prevent the default form submission behavior
    if (judgementForm && judgementFields) { // Only the interviewer page has judgement controls.
      const formData = new FormData(judgementForm);
      const truth = formData.get("truth");
      const confidence = formData.get("confidence");
      if (!truth || !confidence || !confidenceSliderTouched) return;
      judgementFields.disabled = true; // Prevent duplicate submissions while waiting for the server.
      try {
        const response = await fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/judgement`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ room_participant_id: activeRoomParticipantId, judgement: truth, confidence: Number(confidence) }),
        });
        if (!response.ok) throw new Error("Could not submit judgement.");
        // Read the one-time response body once; correctness is present only in the feedback condition.
        const result = await response.json() as { correct?: boolean };
        if (judgementStatus) {
          if (typeof result.correct === "boolean") {
            // Show accuracy feedback and wait for the interviewer to choose Continue.
            judgementStatus.textContent = result.correct
              ? "Your judgement was correct"
              : "Your judgement was incorrect";
            judgementStatus.classList.toggle("judgement-status--correct", result.correct);
            judgementStatus.classList.toggle("judgement-status--incorrect", !result.correct);
            judgementStatus.hidden = false;
            judgementFields.hidden = true;
            if (judgementFeedback) {
              judgementFeedback.hidden = false;
            }
            if (continueButton) {
              continueButton.hidden = false;
              continueButton.focus();
            }
          } else {
            // Control receives no correctness; hide the result and start the same shared countdown immediately.
            judgementStatus.textContent = "";
            judgementStatus.hidden = true;
            judgementFields.hidden = true;
            await continueToNextClaim();
          }
        }
      } catch (error) {
        judgementFields.disabled = false; // Let the interviewer retry when the request fails.
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
        body: JSON.stringify({ room_participant_id: activeRoomParticipantId }),
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

    if (!openingClaimReceived || messageRequestPending) return;
    if (questionTypeSelect && selectingQuestionType) {
      // Submit the category for the already-sent question before enabling the interviewee's reply.
      const questionType = questionTypeSelect.value;
      if (!questionType) {
        questionTypeSelect.reportValidity();
        return;
      }
      messageRequestPending = true;
      questionTypeSelect.disabled = true;
      sendButton.disabled = true;
      void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/question-type`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ room_participant_id: activeRoomParticipantId, question_type: questionType }),
      })
        .then(async (response) => {
          const data = await response.json() as { error?: string };
          if (!response.ok) throw new Error(data.error ?? "Could not save the question type.");
          questionTypeSelect.value = "";
          setQuestionTypeStep(false);
        })
        .catch((error: unknown) => {
          console.error(error instanceof Error ? error.message : "Could not save the question type.");
        })
        .finally(() => {
          // Leave the category available for retry if the server did not accept it.
          messageRequestPending = false;
          questionTypeSelect.disabled = !selectingQuestionType;
          sendButton.disabled = !selectingQuestionType;
          if (selectingQuestionType) questionTypeSelect.focus();
          else if (canSend) input.focus();
        });
      return;
    }
    const text = input.value.trim(); // Get the trimmed text from the input field
    if (!text) return; // Do nothing if the input is empty

    messageRequestPending = true;
    input.disabled = true;
    sendButton.disabled = true;

    void fetch(`/api/rooms/${encodeURIComponent(activeRoomCode)}/messages`, { // Send a new message to the server
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        room_participant_id: activeRoomParticipantId,
        text,
      }),
    }) // Handle the server response for the new message
      .then(async (response) => { // Process the server response for the new message
        const data = await response.json() as {
          message?: {
            id: number;
            room_participant_id: string;
            role: string;
            text: string;
            is_claim?: boolean;
          };
          error?: string;
        };
        if (!response.ok || !data.message) throw new Error(data.error ?? "Could not send message."); // Throw an error if the message could not be sent
        canSend = false; // The server has switched the turn; wait for polling to confirm the next permission
        renderMessage(data.message); // Render the newly sent message in the chat UI
        input.value = ""; // Clear the draft only after the server accepts the message.
        if (questionTypeSelect && questionTypeStep) {
          // Ask for the category only after the server has accepted and displayed the message.
          questionTypeSelect.value = "";
          setQuestionTypeStep(true);
          questionTypeSelect.focus();
        }
      })
      .catch((error: unknown) => {
        console.error(error instanceof Error ? error.message : "Could not send message."); // Log any errors encountered while sending the message
      })
      .finally(() => {
        // Keep controls disabled until both the request and server turn state allow another message.
        messageRequestPending = false;
        input.disabled = selectingQuestionType || !canSend || !openingClaimReceived;
        sendButton.disabled = selectingQuestionType
          ? messageRequestPending
          : !canSend || !openingClaimReceived;
        if (questionTypeSelect) {
          questionTypeSelect.disabled = !selectingQuestionType;
        }
        if (selectingQuestionType) {
          questionTypeSelect?.focus(); // Return focus to the category control if submission failed.
        } else if (openingClaimReceived) {
          input.focus(); // Return focus to the message field after the composer is restored.
        }
      });
  }); // Handle the form submission for sending a new message
}


// polling means repeatedly checking the server for new messages at regular intervals
// In this code, pollMessages is called every second to fetch new messages from the server
// and update the chat UI accordingly
// server pushed messages would be received in real-time without the need for polling and would update the chat UI immediately; server opens a connection to the client and sends new messages as they arrive