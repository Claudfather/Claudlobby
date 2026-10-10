// The same renderer consumes either native host reads or an admitted workspace.
import { createConnectedWorkspace } from "./connected-workspace.js";

const contextElement = globalThis.document?.getElementById('workspace-document-context');
let context;
if (contextElement) {
  try { context = JSON.parse(contextElement.textContent.length <= 16384 ? contextElement.textContent : ''); }
  catch { context = null; }
  contextElement.remove();
}
// Invalid connected metadata must never fall back to unauthenticated host reads.
const connected = contextElement ? createConnectedWorkspace({documentContext:context}) : null;
export const mountSessionControls = connected?.mountSessionControls;
export const dispose = connected?.dispose;
export const interactionContext = connected?.interactionContext;
export const nudgeContext = connected?.nudgeContext;
export const feedbackContext = connected?.feedbackContext;
export const prepareAction = connected?.prepareAction;
export const sendAction = connected?.sendAction;
export const actionReceipt = connected?.actionReceipt;

export async function jget(url) {
  if (connected) return connected.jget(url);
  try {
    const response = await fetch(url);
    return await response.json();
  } catch {
    return null; // renderState(null) => disconnected
  }
}

export function createEventSource(url) {
  if (connected) return connected.createEventSource(url);
  return new EventSource(url);
}
