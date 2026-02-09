use axum::extract::State;
use axum::Json;
use serde_json::{json, Value};

use super::AppState;

pub async fn health_check(State(state): State<AppState>) -> Json<Value> {
    let uptime = state.started_at.elapsed().as_secs();

    Json(json!({
        "status": "healthy",
        "version": env!("CARGO_PKG_VERSION"),
        "uptime_seconds": uptime,
        "checks": {},
        "service": "hide-my-list"
    }))
}
