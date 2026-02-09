pub mod conversation;
pub mod health;

use std::sync::Arc;
use std::time::Instant;

use axum::routing::{get, post};
use axum::Router;
use tower_http::request_id::{MakeRequestUuid, SetRequestIdLayer};
use tower_http::trace::TraceLayer;

use crate::config::Config;
use crate::storage::memory::InMemoryTaskRepository;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub started_at: Instant,
    pub task_repo: Arc<InMemoryTaskRepository>,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        Self {
            config: Arc::new(config),
            started_at: Instant::now(),
            task_repo: Arc::new(InMemoryTaskRepository::new()),
        }
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health::health_check))
        .nest("/api/v1", api_routes())
        .with_state(state)
        .layer(TraceLayer::new_for_http())
        .layer(SetRequestIdLayer::x_request_id(MakeRequestUuid))
}

fn api_routes() -> Router<AppState> {
    Router::new().route("/conversation", post(conversation::converse))
}
