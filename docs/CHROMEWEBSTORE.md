# Chrome Web Store Listing & Extension Documentation

## Basic Metadata

- **Name**: Automata Browser Assistant
- **Short Name**: Automata
- **Summary**: Connects your Chrome browser session to the Automata service for autonomous browsing and task execution.
- **Version**: 1.0.0
- **Category**: Productivity / Developer Tools
- **Manifest Version**: 3

---

## Permissions Justification

| Permission | Justification |
| :--- | :--- |
| `tabs` | Required to create dedicated task tabs for automation, query tab properties (such as URL and title), and track tab lifecycle events (e.g. detecting when a task tab is closed). |
| `scripting` | Required to inject the content script (`content.js`) into task tabs to perform DOM element inspection and execute user-directed browser actions. |
| `storage` | Used locally in Chrome to store the backend WebSocket server URL and persist active task-to-tab mappings across service worker suspensions. |
| `host_permissions: ["<all_urls>"]` | Required so the Automata can navigate to and interact with any website requested by the user during automation tasks. |

---

## Privacy & Security Disclosures

- **Zero Credential Collection**:
  - Does NOT collect, read, transmit, or store user passwords.
  - Does NOT collect, extract, copy, or transmit HTTP cookies or authentication session tokens.
  - Does NOT require or access local Chrome profile files.
- **Data in Transit**:
  - Browser commands and DOM structure representations are transmitted strictly between the local extension and the user-specified FastAPI backend (by default on `localhost:8000`).
- **User Control**:
  - The extension only operates in tabs explicitly created for automation tasks.
  - Users can disconnect the bridge at any time via the extension popup.
