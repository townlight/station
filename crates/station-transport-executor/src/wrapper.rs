//! Public stationd entry point for the storage-owned transport authority.

pub use station_storage::{
    AuthenticatedReceiver, HttpsReceiverAdapter, ProofBinding, ReceiverProofExecutor,
    run_transport_proof_queue_controller,
};
