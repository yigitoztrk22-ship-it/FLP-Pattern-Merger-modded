//! Error type shared by every stage of the merge.

use std::fmt;

/// Every failure mode the merger can report.
#[derive(Debug)]
pub enum MergeError {
    /// The file is not a supported/valid FLP container.
    Format(String),
    /// Bad arguments or a request the tool refuses (same paths, wrong suffix…).
    Usage(String),
    /// The caller asked the worker to stop.
    Cancelled,
    Io(std::io::Error),
}

pub type Result<T> = std::result::Result<T, MergeError>;

impl MergeError {
    pub fn format(message: impl Into<String>) -> Self {
        MergeError::Format(message.into())
    }

    pub fn usage(message: impl Into<String>) -> Self {
        MergeError::Usage(message.into())
    }
}

impl fmt::Display for MergeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MergeError::Format(message) | MergeError::Usage(message) => f.write_str(message),
            MergeError::Cancelled => f.write_str("Cancelled by user."),
            MergeError::Io(err) => write!(f, "{err}"),
        }
    }
}

impl std::error::Error for MergeError {}

impl From<std::io::Error> for MergeError {
    fn from(err: std::io::Error) -> Self {
        MergeError::Io(err)
    }
}
