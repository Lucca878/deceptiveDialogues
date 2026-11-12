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

const role = sessionStorage.getItem("chat-role"); // User's role in the chat (interviewer or interviewee)
const roomCode = sessionStorage.getItem("chat-room-code"); // Active chat room code
const participantId = sessionStorage.getItem("participant-id"); // Unique identifier for the participant
if (
  (role !== "interviewer" && role !== "interviewee") ||
  !roomCode ||
  !participantId
) {
  window.location.replace("/room"); // Redirect to the room selection page if required session data is missing
} else {
  const activeRoomCode = roomCode!;
  const activeParticipantId = participantId!;
  roleName.textContent = role === "interviewer" ? "Interviewer" : "Interviewee";
  let lastMessageId = 0;
  let lastTypingPing = 0;
  let openingClaimReceived = false;
  const renderedMessageIds = new Set<number>();

  function pingTyping(): void { // Sends a typing ping to the server to indicate that the participant is currently typing
    if (!openingClaimReceived) return;
    const now = Date.now();
    if (now - lastTypingPing < 2000) return;
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
  }): void {
    if (renderedMessageIds.has(message.id)) return;
    renderedMessageIds.add(message.id);
    emptyState.remove(); // Remove the empty state message when a new message is rendered

    const bubble = document.createElement("p"); // Create a new paragraph element for the chat message
    bubble.className = "chat-message"; // Apply the base chat message styling
    if (message.participant_id !== activeParticipantId) {
      bubble.classList.add("chat-message--remote"); // Style the message as a remote message if it's from the other participant
    }
    bubble.textContent = message.text; // Set the text content of the chat message bubble
    messages.append(bubble); // Append the chat message bubble to the messages container
    if (message.id === 1 && message.role === "interviewee") {
      openingClaimReceived = true;
      input.disabled = false;
      input.placeholder = "Write a message...";
      sendButton.disabled = false;
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
        }>; // Array of new messages from the server
        partner_typing?: boolean;
        error?: string;
      };
      if (!response.ok) throw new Error(data.error ?? "Could not load messages."); // Throw an error if the response is not OK

      typingIndicator.hidden = !data.partner_typing; // Show or hide the typing indicator based on the partner's typing status

      for (const message of data.messages ?? []) {
        lastMessageId = Math.max(lastMessageId, message.id);
        renderMessage(message); // Render the new message in the chat UI
      }
    } catch (error) {
      console.error(error instanceof Error ? error.message : "Could not load messages."); // Log any errors encountered during message polling
    } finally {
      window.setTimeout(() => void pollMessages(), 1000); // Schedule the next poll after 1 second
    }
  }

  void pollMessages(); // Start polling for new messages immediately

  input.addEventListener("input", pingTyping);

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
          };
          error?: string;
        };
        if (!response.ok || !data.message) throw new Error(data.error ?? "Could not send message."); // Throw an error if the message could not be sent
        renderMessage(data.message); // Render the newly sent message in the chat UI
      })
      .catch((error: unknown) => {
        input.value = text;
        console.error(error instanceof Error ? error.message : "Could not send message."); // Log any errors encountered while sending the message
      })
      .finally(() => {
        input.disabled = !openingClaimReceived;
        sendButton.disabled = !openingClaimReceived;
        if (openingClaimReceived) input.focus(); // Focus the input field after sending a message
      });
  }); // Handle the form submission for sending a new message
}


// polling means repeatedly checking the server for new messages at regular intervals
// In this code, pollMessages is called every second to fetch new messages from the server
// and update the chat UI accordingly
// server pushed messages would be received in real-time without the need for polling and would update the chat UI immediately; server opens a connection to the client and sends new messages as they arrive