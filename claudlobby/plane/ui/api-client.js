// Same-origin read transport for the canonical Plane. Disposable browser
// experiments can serve a different module without duplicating the renderer.
export async function jget(url) {
  try {
    const response = await fetch(url);
    return await response.json();
  } catch {
    return null; // renderState(null) => disconnected
  }
}

export function createEventSource(url) {
  return new EventSource(url);
}
