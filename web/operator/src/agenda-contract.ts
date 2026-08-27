export type AgendaRequestToken = {
  generation: number;
  agendaId: string;
  channelId: string;
};

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
