use hide_my_list::api::{self, AppState};
use hide_my_list::config::Config;

fn test_config() -> Config {
    Config {
        port: 0,
        log_level: "info".to_string(),
        environment: "test".to_string(),
    }
}

#[tokio::test]
async fn health_endpoint_returns_200() {
    let state = AppState::new(test_config());
    let app = api::router(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let response = reqwest::get(format!("http://{addr}/health"))
        .await
        .unwrap();

    assert_eq!(response.status(), 200);

    let body: serde_json::Value = response.json().await.unwrap();
    assert_eq!(body["status"], "healthy");
    assert_eq!(body["version"], env!("CARGO_PKG_VERSION"));
    assert!(body["uptime_seconds"].is_number());
}
