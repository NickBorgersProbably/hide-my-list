use axum::Json;
use serde::{Deserialize, Serialize};

use crate::error::AppError;

#[derive(Deserialize)]
pub struct ConversationRequest {
    pub message: String,
}

#[derive(Serialize)]
pub struct ConversationResponse {
    pub response: String,
    pub intent: Option<String>,
    pub task: Option<serde_json::Value>,
}

pub async fn converse(
    Json(req): Json<ConversationRequest>,
) -> Result<Json<ConversationResponse>, AppError> {
    if req.message.trim().is_empty() {
        return Err(AppError::BadRequest("message cannot be empty".into()));
    }

    tracing::info!(message = %req.message, "received conversation message");

    Ok(Json(ConversationResponse {
        response: format!(
            "Hello! You said: \"{}\". I'm not smart enough to help with that yet, but I will be soon!",
            req.message
        ),
        intent: None,
        task: None,
    }))
}
