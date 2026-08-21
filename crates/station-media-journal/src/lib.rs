use std::fs::{File, OpenOptions};
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::Path;

use station_media_protocol::{
    MAX_FRAME_BYTES, ProtocolError, WorkerEventEnvelope, decode_event_frame, encode_event_frame,
};

const MAGIC: [u8; 4] = *b"TLJR";
const JOURNAL_VERSION: u16 = 1;
const HEADER_BYTES: usize = 16;
const MAX_EVENT_FRAME_BYTES: usize = MAX_FRAME_BYTES + 4;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum JournalError {
    Io(String),
    Corrupt { offset: u64, reason: String },
    WrongIdentity,
    NonMonotonic { expected: u64, actual: u64 },
    Protocol(ProtocolError),
}

pub struct JournalWriter {
    file: File,
    worker_id: String,
    channel_id: String,
    next_sequence: u64,
}

impl JournalWriter {
    pub fn open(
        path: impl AsRef<Path>,
        worker_id: &str,
        channel_id: &str,
    ) -> Result<Self, JournalError> {
        let existing = read_events(&path)?;
        if existing
            .iter()
            .any(|event| event.worker_id != worker_id || event.channel_id != channel_id)
        {
            return Err(JournalError::WrongIdentity);
        }
        let next_sequence = existing.last().map_or(1, |event| event.sequence + 1);
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(io_error)?;
        Ok(Self {
            file,
            worker_id: worker_id.to_string(),
            channel_id: channel_id.to_string(),
            next_sequence,
        })
    }

    pub fn next_sequence(&self) -> u64 {
        self.next_sequence
    }

    pub fn append(&mut self, event: &WorkerEventEnvelope) -> Result<(), JournalError> {
        if event.worker_id != self.worker_id || event.channel_id != self.channel_id {
            return Err(JournalError::WrongIdentity);
        }
        if event.sequence != self.next_sequence {
            return Err(JournalError::NonMonotonic {
                expected: self.next_sequence,
                actual: event.sequence,
            });
        }

        let payload = encode_event_frame(event).map_err(JournalError::Protocol)?;
        decode_event_frame(&payload).map_err(JournalError::Protocol)?;
        let payload_length =
            u32::try_from(payload.len()).expect("a bounded event frame always fits in a u32");
        let mut record = Vec::with_capacity(HEADER_BYTES + payload.len());
        record.extend_from_slice(&MAGIC);
        record.extend_from_slice(&JOURNAL_VERSION.to_le_bytes());
        record.extend_from_slice(&0_u16.to_le_bytes());
        record.extend_from_slice(&payload_length.to_le_bytes());
        record.extend_from_slice(&crc32(&payload).to_le_bytes());
        record.extend_from_slice(&payload);

        self.file.write_all(&record).map_err(io_error)?;
        self.file.flush().map_err(io_error)?;
        self.file.sync_data().map_err(io_error)?;
        self.next_sequence += 1;
        Ok(())
    }
}

pub fn read_events(path: impl AsRef<Path>) -> Result<Vec<WorkerEventEnvelope>, JournalError> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(io_error(error)),
    };
    file.seek(SeekFrom::Start(0)).map_err(io_error)?;
    let mut events = Vec::new();
    let mut offset = 0_u64;
    let mut identity: Option<(String, String)> = None;
    let mut expected_sequence = 1_u64;

    loop {
        let mut header = [0_u8; HEADER_BYTES];
        match file.read(&mut header[..1]) {
            Ok(0) => break,
            Ok(1) => {}
            Ok(_) => unreachable!("the read buffer has one byte"),
            Err(error) => return Err(io_error(error)),
        }
        read_exact_record(
            &mut file,
            &mut header[1..],
            offset,
            "truncated record header",
        )?;
        if header[..4] != MAGIC {
            return Err(corrupt(offset, "invalid journal magic"));
        }
        let version = u16::from_le_bytes(header[4..6].try_into().expect("two header bytes"));
        if version != JOURNAL_VERSION {
            return Err(corrupt(
                offset,
                format!("unsupported journal version {version}"),
            ));
        }
        if header[6..8] != [0, 0] {
            return Err(corrupt(offset, "reserved header bits are not zero"));
        }
        let payload_length =
            u32::from_le_bytes(header[8..12].try_into().expect("four header bytes")) as usize;
        if payload_length > MAX_EVENT_FRAME_BYTES {
            return Err(corrupt(
                offset,
                format!("event frame is too large: {payload_length}"),
            ));
        }
        let expected_checksum =
            u32::from_le_bytes(header[12..16].try_into().expect("four header bytes"));
        let mut payload = vec![0_u8; payload_length];
        read_exact_record(&mut file, &mut payload, offset, "truncated event frame")?;
        if crc32(&payload) != expected_checksum {
            return Err(corrupt(offset, "event frame checksum mismatch"));
        }
        let event = decode_event_frame(&payload).map_err(JournalError::Protocol)?;
        let event_identity = (event.worker_id.clone(), event.channel_id.clone());
        match &identity {
            Some(expected) if expected != &event_identity => {
                return Err(corrupt(offset, "worker or channel identity changed"));
            }
            None => identity = Some(event_identity),
            _ => {}
        }
        if event.sequence != expected_sequence {
            return Err(JournalError::NonMonotonic {
                expected: expected_sequence,
                actual: event.sequence,
            });
        }
        expected_sequence += 1;
        events.push(event);
        offset +=
            u64::try_from(HEADER_BYTES + payload_length).expect("record length always fits in u64");
    }
    Ok(events)
}

fn read_exact_record(
    file: &mut File,
    bytes: &mut [u8],
    offset: u64,
    truncated_reason: &str,
) -> Result<(), JournalError> {
    match file.read_exact(bytes) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => {
            Err(corrupt(offset, truncated_reason))
        }
        Err(error) => Err(io_error(error)),
    }
}

fn crc32(bytes: &[u8]) -> u32 {
    let mut checksum = u32::MAX;
    for byte in bytes {
        checksum ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = 0_u32.wrapping_sub(checksum & 1);
            checksum = (checksum >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !checksum
}

fn corrupt(offset: u64, reason: impl Into<String>) -> JournalError {
    JournalError::Corrupt {
        offset,
        reason: reason.into(),
    }
}

fn io_error(error: std::io::Error) -> JournalError {
    JournalError::Io(error.to_string())
}
