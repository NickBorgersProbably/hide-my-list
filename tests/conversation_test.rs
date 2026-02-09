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
async fn conversation_echoes_message() {
    let state = AppState::new(test_config());
    let app = api::router(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let client = reqwest::Client::new();
    let response = client
        .post(format!("http://{addr}/api/v1/conversation"))
        .json(&serde_json::json!({ "message": "hello" }))
        .send()
        .await
        .unwrap();

    assert_eq!(response.status(), 200);

    let body: serde_json::Value = response.json().await.unwrap();
    assert!(body["response"].as_str().unwrap().contains("hello"));
    assert!(body["intent"].is_null());
    assert!(body["task"].is_null());
}

#[tokio::test]
async fn conversation_rejects_empty_message() {
    let state = AppState::new(test_config());
    let app = api::router(state);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();

    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let client = reqwest::Client::new();
    let response = client
        .post(format!("http://{addr}/api/v1/conversation"))
        .json(&serde_json::json!({ "message": "   " }))
        .send()
        .await
        .unwrap();

    assert_eq!(response.status(), 400);
}
