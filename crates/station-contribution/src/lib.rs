use serde::{Deserialize, Serialize};

const MAX_ENDPOINT_LEN: usize = 256;
const MAX_INVITATION_LIFETIME_MS: i64 = 7 * 24 * 60 * 60 * 1_000;
const MAX_RECEIPTS_PER_SESSION: usize = 256;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ContributionRole {
    Operator,
    Contributor,
    Worker,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Actor {
    pub actor_id: String,
    pub role: ContributionRole,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum RoomState {
    Draft,
    Open,
    Closed,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum InvitationState {
    Issued,
    Claimed {
        session_id: String,
        claimed_at_utc_ms: i64,
    },
    Revoked {
        revoked_at_utc_ms: i64,
    },
    Expired {
        expired_at_utc_ms: i64,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum SessionState {
    AwaitingWorker,
    Connecting,
    Connected,
    Ended,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ReceiptKind {
    Connecting,
    Connected,
    Ended,
    Failed,
    RecoveryStopped,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TurnIntent {
    pub relay_uri: String,
    pub credential_reference_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct VdoNinjaBridgeIntent {
    pub configuration_id: String,
    pub signalling_origin: String,
    pub turn: Option<TurnIntent>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContributionRoom {
    pub room_id: String,
    pub channel_id: String,
    pub created_by_operator: String,
    pub bridge: VdoNinjaBridgeIntent,
    pub state: RoomState,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Invitation {
    pub invitation_id: String,
    pub room_id: String,
    pub contributor_id: String,
    pub issued_by_operator: String,
    pub issued_at_utc_ms: i64,
    pub expires_at_utc_ms: i64,
    pub state: InvitationState,
    pub revision: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContributionSession {
    pub session_id: String,
    pub room_id: String,
    pub invitation_id: String,
    pub contributor_id: String,
    pub worker_id: String,
    pub bridge_configuration_id: String,
    pub state: SessionState,
    pub created_at_utc_ms: i64,
    pub revision: u64,
    pub receipt_count: u16,
    pub last_receipt_sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ConnectionReceipt {
    pub receipt_id: String,
    pub room_id: String,
    pub session_id: String,
    pub invitation_id: String,
    pub worker_id: String,
    pub sequence: u64,
    pub kind: ReceiptKind,
    pub observed_at_utc_ms: i64,
}

impl ContributionRoom {
    pub fn validate_create(&self) -> Result<(), String> {
        validate_uuid(&self.room_id, "contribution room identity")?;
        validate_uuid(&self.channel_id, "contribution room channel identity")?;
        validate_uuid(
            &self.created_by_operator,
            "contribution room operator identity",
        )?;
        validate_bridge(&self.bridge)?;
        if self.revision != 1 || self.state != RoomState::Draft {
            return Err("new contribution room must be revision-one draft authority".into());
        }
        Ok(())
    }

    pub fn validate_successor_of(
        &self,
        current: &ContributionRoom,
        expected_revision: u64,
    ) -> Result<(), String> {
        current.validate_current()?;
        self.validate_current()?;
        if expected_revision != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("room revision overflow")?
        {
            return Err("contribution room compare-and-set revision conflict".into());
        }
        if self.room_id != current.room_id
            || self.channel_id != current.channel_id
            || self.created_by_operator != current.created_by_operator
            || self.bridge != current.bridge
        {
            return Err("contribution room identity or bridge authority cannot be rebound".into());
        }
        if !matches!(
            (current.state, self.state),
            (RoomState::Draft, RoomState::Open)
                | (RoomState::Open, RoomState::Closed)
                | (RoomState::Open, RoomState::Failed)
        ) {
            return Err("contribution room lifecycle transition is invalid or terminal".into());
        }
        Ok(())
    }

    fn validate_current(&self) -> Result<(), String> {
        validate_uuid(&self.room_id, "contribution room identity")?;
        validate_uuid(&self.channel_id, "contribution room channel identity")?;
        validate_uuid(
            &self.created_by_operator,
            "contribution room operator identity",
        )?;
        validate_bridge(&self.bridge)?;
        if self.revision == 0 {
            return Err("contribution room revision is invalid".into());
        }
        Ok(())
    }
}

pub fn open_room(
    current: &ContributionRoom,
    expected_revision: u64,
    operator: &Actor,
) -> Result<ContributionRoom, String> {
    require_actor(
        operator,
        ContributionRole::Operator,
        &current.created_by_operator,
    )?;
    let mut next = current.clone();
    next.state = RoomState::Open;
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("room revision overflow")?;
    next.validate_successor_of(current, expected_revision)?;
    Ok(next)
}

pub fn close_room(
    current: &ContributionRoom,
    expected_revision: u64,
    operator: &Actor,
) -> Result<ContributionRoom, String> {
    require_actor(
        operator,
        ContributionRole::Operator,
        &current.created_by_operator,
    )?;
    let mut next = current.clone();
    next.state = RoomState::Closed;
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("room revision overflow")?;
    next.validate_successor_of(current, expected_revision)?;
    Ok(next)
}

pub fn issue_invitation(
    room: &ContributionRoom,
    operator: &Actor,
    invitation: Invitation,
) -> Result<Invitation, String> {
    room.validate_current()?;
    require_actor(
        operator,
        ContributionRole::Operator,
        &room.created_by_operator,
    )?;
    if room.state != RoomState::Open {
        return Err("contribution invitations require an open room".into());
    }
    invitation.validate_create_for(room)?;
    Ok(invitation)
}

pub fn claim_invitation(
    current: &Invitation,
    expected_revision: u64,
    contributor: &Actor,
    session_id: &str,
    claimed_at_utc_ms: i64,
) -> Result<Invitation, String> {
    current.validate_current()?;
    require_actor(
        contributor,
        ContributionRole::Contributor,
        &current.contributor_id,
    )?;
    validate_uuid(session_id, "contribution session identity")?;
    if expected_revision != current.revision
        || current.state != InvitationState::Issued
        || claimed_at_utc_ms < current.issued_at_utc_ms
        || claimed_at_utc_ms >= current.expires_at_utc_ms
    {
        return Err("contribution invitation claim is stale, replayed, or expired".into());
    }
    let mut next = current.clone();
    next.state = InvitationState::Claimed {
        session_id: session_id.to_owned(),
        claimed_at_utc_ms,
    };
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("invitation revision overflow")?;
    next.validate_successor_of(current, expected_revision)?;
    Ok(next)
}

pub fn revoke_invitation(
    current: &Invitation,
    expected_revision: u64,
    operator: &Actor,
    revoked_at_utc_ms: i64,
) -> Result<Invitation, String> {
    current.validate_current()?;
    require_actor(
        operator,
        ContributionRole::Operator,
        &current.issued_by_operator,
    )?;
    if expected_revision != current.revision
        || current.state != InvitationState::Issued
        || revoked_at_utc_ms < current.issued_at_utc_ms
    {
        return Err("contribution invitation revoke is stale or terminal".into());
    }
    let mut next = current.clone();
    next.state = InvitationState::Revoked { revoked_at_utc_ms };
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("invitation revision overflow")?;
    next.validate_successor_of(current, expected_revision)?;
    Ok(next)
}

pub fn expire_invitation(
    current: &Invitation,
    expected_revision: u64,
    expired_at_utc_ms: i64,
) -> Result<Invitation, String> {
    current.validate_current()?;
    if expected_revision != current.revision
        || current.state != InvitationState::Issued
        || expired_at_utc_ms < current.expires_at_utc_ms
    {
        return Err("contribution invitation expiry is stale, early, or terminal".into());
    }
    let mut next = current.clone();
    next.state = InvitationState::Expired { expired_at_utc_ms };
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("invitation revision overflow")?;
    next.validate_successor_of(current, expected_revision)?;
    Ok(next)
}

pub fn create_session(
    room: &ContributionRoom,
    invitation: &Invitation,
    operator: &Actor,
    worker: &Actor,
    session_id: String,
    created_at_utc_ms: i64,
) -> Result<ContributionSession, String> {
    room.validate_current()?;
    invitation.validate_current()?;
    require_actor(
        operator,
        ContributionRole::Operator,
        &room.created_by_operator,
    )?;
    require_actor(worker, ContributionRole::Worker, &worker.actor_id)?;
    if room.state != RoomState::Open || invitation.room_id != room.room_id {
        return Err("contribution session requires exact open room authority".into());
    }
    let InvitationState::Claimed {
        session_id: claimed_session_id,
        claimed_at_utc_ms,
    } = &invitation.state
    else {
        return Err("contribution session requires one claimed invitation".into());
    };
    if claimed_session_id != &session_id || created_at_utc_ms < *claimed_at_utc_ms {
        return Err(
            "contribution session identity or time is not bound to invitation claim".into(),
        );
    }
    let session = ContributionSession {
        session_id,
        room_id: room.room_id.clone(),
        invitation_id: invitation.invitation_id.clone(),
        contributor_id: invitation.contributor_id.clone(),
        worker_id: worker.actor_id.clone(),
        bridge_configuration_id: room.bridge.configuration_id.clone(),
        state: SessionState::AwaitingWorker,
        created_at_utc_ms,
        revision: 1,
        receipt_count: 0,
        last_receipt_sequence: 0,
    };
    session.validate_for(room, invitation)?;
    Ok(session)
}

pub fn apply_connection_receipt(
    room: &ContributionRoom,
    invitation: &Invitation,
    current: &ContributionSession,
    expected_revision: u64,
    worker: &Actor,
    prior_receipts: &[ConnectionReceipt],
    receipt: ConnectionReceipt,
) -> Result<ContributionSession, String> {
    room.validate_current()?;
    invitation.validate_current()?;
    current.validate_for(room, invitation)?;
    require_actor(worker, ContributionRole::Worker, &current.worker_id)?;
    if room.state != RoomState::Open || expected_revision != current.revision {
        return Err("contribution session room or compare-and-set authority is stale".into());
    }
    validate_receipt_ledger(room, invitation, current, prior_receipts)?;
    receipt.validate_for(room, invitation, current)?;
    if receipt.sequence
        != current
            .last_receipt_sequence
            .checked_add(1)
            .ok_or("receipt sequence overflow")?
        || prior_receipts
            .iter()
            .any(|prior| prior.receipt_id == receipt.receipt_id)
        || receipt.observed_at_utc_ms < current.created_at_utc_ms
        || prior_receipts
            .last()
            .is_some_and(|prior| receipt.observed_at_utc_ms < prior.observed_at_utc_ms)
    {
        return Err("contribution receipt is replayed, out of order, or time-invalid".into());
    }
    let next_state = next_session_state(current.state, receipt.kind)?;
    let mut next = current.clone();
    next.state = next_state;
    next.revision = current
        .revision
        .checked_add(1)
        .ok_or("contribution session revision overflow")?;
    next.receipt_count = current
        .receipt_count
        .checked_add(1)
        .ok_or("contribution receipt count overflow")?;
    next.last_receipt_sequence = receipt.sequence;
    next.validate_for(room, invitation)?;
    Ok(next)
}

impl Invitation {
    fn validate_create_for(&self, room: &ContributionRoom) -> Result<(), String> {
        self.validate_current()?;
        if self.revision != 1
            || self.state != InvitationState::Issued
            || self.room_id != room.room_id
        {
            return Err("new contribution invitation authority is invalid".into());
        }
        Ok(())
    }

    fn validate_successor_of(
        &self,
        current: &Invitation,
        expected_revision: u64,
    ) -> Result<(), String> {
        current.validate_current()?;
        self.validate_current()?;
        if expected_revision != current.revision
            || self.revision
                != current
                    .revision
                    .checked_add(1)
                    .ok_or("invitation revision overflow")?
            || self.invitation_id != current.invitation_id
            || self.room_id != current.room_id
            || self.contributor_id != current.contributor_id
            || self.issued_by_operator != current.issued_by_operator
            || self.issued_at_utc_ms != current.issued_at_utc_ms
            || self.expires_at_utc_ms != current.expires_at_utc_ms
        {
            return Err("contribution invitation revision or identity cannot be rebound".into());
        }
        if !matches!(
            (&current.state, &self.state),
            (InvitationState::Issued, InvitationState::Claimed { .. })
                | (InvitationState::Issued, InvitationState::Revoked { .. })
                | (InvitationState::Issued, InvitationState::Expired { .. })
        ) {
            return Err(
                "contribution invitation lifecycle transition is invalid or terminal".into(),
            );
        }
        Ok(())
    }

    fn validate_current(&self) -> Result<(), String> {
        validate_uuid(&self.invitation_id, "contribution invitation identity")?;
        validate_uuid(&self.room_id, "contribution invitation room identity")?;
        validate_uuid(&self.contributor_id, "contribution contributor identity")?;
        validate_uuid(
            &self.issued_by_operator,
            "contribution invitation operator identity",
        )?;
        if self.revision == 0
            || self.issued_at_utc_ms < 0
            || self.expires_at_utc_ms <= self.issued_at_utc_ms
            || self.expires_at_utc_ms - self.issued_at_utc_ms > MAX_INVITATION_LIFETIME_MS
        {
            return Err("contribution invitation revision or bounded lifetime is invalid".into());
        }
        match &self.state {
            InvitationState::Issued => Ok(()),
            InvitationState::Claimed {
                session_id,
                claimed_at_utc_ms,
            } if validate_uuid(session_id, "claimed contribution session identity").is_ok()
                && *claimed_at_utc_ms >= self.issued_at_utc_ms
                && *claimed_at_utc_ms < self.expires_at_utc_ms =>
            {
                Ok(())
            }
            InvitationState::Revoked { revoked_at_utc_ms }
                if *revoked_at_utc_ms >= self.issued_at_utc_ms =>
            {
                Ok(())
            }
            InvitationState::Expired { expired_at_utc_ms }
                if *expired_at_utc_ms >= self.expires_at_utc_ms =>
            {
                Ok(())
            }
            _ => Err("contribution invitation lifecycle data is invalid".into()),
        }
    }
}

impl ContributionSession {
    fn validate_for(&self, room: &ContributionRoom, invitation: &Invitation) -> Result<(), String> {
        validate_uuid(&self.session_id, "contribution session identity")?;
        validate_uuid(&self.worker_id, "contribution session worker identity")?;
        if self.revision == 0
            || self.created_at_utc_ms < 0
            || self.receipt_count as usize > MAX_RECEIPTS_PER_SESSION
            || self.last_receipt_sequence != u64::from(self.receipt_count)
            || self.room_id != room.room_id
            || self.invitation_id != invitation.invitation_id
            || self.contributor_id != invitation.contributor_id
            || self.bridge_configuration_id != room.bridge.configuration_id
        {
            return Err(
                "contribution session authority is malformed, rebound, or unbounded".into(),
            );
        }
        let InvitationState::Claimed { session_id, .. } = &invitation.state else {
            return Err("contribution session has no claimed invitation authority".into());
        };
        if session_id != &self.session_id {
            return Err("contribution session identity differs from invitation claim".into());
        }
        Ok(())
    }
}

impl ConnectionReceipt {
    fn validate_for(
        &self,
        room: &ContributionRoom,
        invitation: &Invitation,
        session: &ContributionSession,
    ) -> Result<(), String> {
        validate_uuid(&self.receipt_id, "contribution receipt identity")?;
        if self.sequence == 0
            || self.observed_at_utc_ms < 0
            || self.room_id != room.room_id
            || self.session_id != session.session_id
            || self.invitation_id != invitation.invitation_id
            || self.worker_id != session.worker_id
        {
            return Err("contribution receipt authority is malformed or mismatched".into());
        }
        Ok(())
    }
}

fn validate_receipt_ledger(
    room: &ContributionRoom,
    invitation: &Invitation,
    session: &ContributionSession,
    receipts: &[ConnectionReceipt],
) -> Result<(), String> {
    if receipts.len() != usize::from(session.receipt_count)
        || receipts.len() > MAX_RECEIPTS_PER_SESSION
    {
        return Err("contribution receipt ledger length is inconsistent or unbounded".into());
    }
    let mut derived_state = SessionState::AwaitingWorker;
    for (index, receipt) in receipts.iter().enumerate() {
        receipt.validate_for(room, invitation, session)?;
        if receipt.sequence != index as u64 + 1
            || receipts[..index]
                .iter()
                .any(|prior| prior.receipt_id == receipt.receipt_id)
            || (index > 0 && receipt.observed_at_utc_ms < receipts[index - 1].observed_at_utc_ms)
        {
            return Err(
                "contribution receipt ledger is not immutable append-only authority".into(),
            );
        }
        derived_state = next_session_state(derived_state, receipt.kind)?;
    }
    if derived_state != session.state {
        return Err("contribution receipt ledger does not derive current session state".into());
    }
    Ok(())
}

fn next_session_state(current: SessionState, receipt: ReceiptKind) -> Result<SessionState, String> {
    match (current, receipt) {
        (SessionState::AwaitingWorker, ReceiptKind::Connecting) => Ok(SessionState::Connecting),
        (SessionState::Connecting, ReceiptKind::Connected) => Ok(SessionState::Connected),
        (SessionState::Connecting | SessionState::Connected, ReceiptKind::Ended) => {
            Ok(SessionState::Ended)
        }
        (
            SessionState::AwaitingWorker | SessionState::Connecting | SessionState::Connected,
            ReceiptKind::Failed | ReceiptKind::RecoveryStopped,
        ) => Ok(SessionState::Failed),
        _ => Err("contribution session receipt transition is invalid or terminal".into()),
    }
}

fn validate_bridge(bridge: &VdoNinjaBridgeIntent) -> Result<(), String> {
    validate_uuid(
        &bridge.configuration_id,
        "VDO.Ninja bridge configuration identity",
    )?;
    validate_endpoint(&bridge.signalling_origin, "https://")?;
    if let Some(turn) = &bridge.turn {
        validate_endpoint(&turn.relay_uri, "turns://")?;
        validate_uuid(
            &turn.credential_reference_id,
            "TURN credential reference identity",
        )?;
    }
    Ok(())
}

fn validate_endpoint(value: &str, required_scheme: &str) -> Result<(), String> {
    let Some(host_port) = value.strip_prefix(required_scheme) else {
        return Err("contribution endpoint scheme is not allowed".into());
    };
    if value.len() > MAX_ENDPOINT_LEN
        || host_port.is_empty()
        || host_port
            .bytes()
            .any(|byte| byte.is_ascii_whitespace() || matches!(byte, b'/' | b'?' | b'#' | b'@'))
        || !host_port.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b':' | b'[' | b']')
        })
    {
        return Err("contribution endpoint is unbounded, secret-bearing, or malformed".into());
    }
    Ok(())
}

fn require_actor(actor: &Actor, role: ContributionRole, identity: &str) -> Result<(), String> {
    validate_uuid(&actor.actor_id, "contribution actor identity")?;
    if actor.role != role || actor.actor_id != identity {
        return Err("contribution actor role or identity is not authorized by authority".into());
    }
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<(), String> {
    let bytes = value.as_bytes();
    if bytes.len() == 36
        && [8, 13, 18, 23].iter().all(|index| bytes[*index] == b'-')
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| [8, 13, 18, 23].contains(&index) || byte.is_ascii_hexdigit())
    {
        Ok(())
    } else {
        Err(format!("{field} must be a bounded UUID"))
    }
}
