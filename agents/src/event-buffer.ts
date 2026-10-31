/** Per-session SSE replay for the workbench.

Each chat session owns its own buffer. A subscriber is given only the frames
that were pushed into that session's buffer, so one session cannot replay
another session's events. Frames posted while nobody is listening are kept,
oldest first, and dropped once the cap is passed.
*/

export const EVENT_BUFFER_CAP = 200;

export function rememberEvent(buffer: string[], frame: string, cap = EVENT_BUFFER_CAP): void {
  buffer.push(frame);
  while (buffer.length > cap) buffer.shift();
}

/** Remove and return every frame waiting on this session. */
export function takeEvents(buffer: string[]): string[] {
  return buffer.splice(0, buffer.length);
}
