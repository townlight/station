export type AgendaRequestToken = {
  generation: number;
  agendaId: string;
  channelId: string;
};

export const AGENDA_TITLE_MAX_UTF8_BYTES = 200;
export const AGENDA_IMPORT_MAX_UTF8_BYTES = 65_536;
export const AGENDA_RAW_TITLE_INPUT_MAX_UTF8_BYTES = 4_096;

const utf8Encoder = new TextEncoder();

export function utf8ByteLength(value: string): number {
  return utf8Encoder.encode(value).byteLength;
}

export function normalizeAgendaTitle(value: string): string {
  return value.trim();
}

export function boundedAgendaTitleInput(
  currentValue: string,
  incomingValue: string,
): { value: string; rejected: boolean } {
  if (utf8ByteLength(incomingValue) <= AGENDA_RAW_TITLE_INPUT_MAX_UTF8_BYTES) {
    return { value: incomingValue, rejected: false };
  }
  const normalized = normalizeAgendaTitle(incomingValue);
  return utf8ByteLength(normalized) <= AGENDA_TITLE_MAX_UTF8_BYTES
    ? { value: normalized, rejected: false }
    : { value: currentValue, rejected: true };
}

export function agendaTextByteError(value: string, maximum: number, field: string): string | null {
  const actual = utf8ByteLength(value);
  return actual <= maximum
    ? null
    : `${field} uses ${actual.toLocaleString("en-US")} UTF-8 bytes; the maximum is ${maximum.toLocaleString("en-US")} UTF-8 bytes.`;
}

export function isCurrentAgendaRequest(
  token: AgendaRequestToken,
  currentGeneration: number,
  currentChannelId: string,
  returnedAgendaId: string,
  returnedChannelId: string,
): boolean {
  return token.generation === currentGeneration
    && token.agendaId === returnedAgendaId
    && token.channelId === currentChannelId
    && returnedChannelId === currentChannelId;
}
