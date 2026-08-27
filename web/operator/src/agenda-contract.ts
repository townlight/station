export type AgendaRequestToken = {
  generation: number;
  agendaId: string;
  channelId: string;
};

export const AGENDA_TITLE_MAX_UTF8_BYTES = 200;
export const AGENDA_IMPORT_MAX_UTF8_BYTES = 65_536;

const utf8Encoder = new TextEncoder();

export function utf8ByteLength(value: string): number {
  return utf8Encoder.encode(value).byteLength;
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
