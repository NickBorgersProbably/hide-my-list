use crate::error::AppError;

#[derive(Debug, Clone)]
pub struct Config {
    pub port: u16,
    pub log_level: String,
    pub environment: String,
}

impl Config {
    pub fn from_env() -> Result<Self, AppError> {
        Ok(Config {
            port: std::env::var("PORT")
                .unwrap_or_else(|_| "8080".to_string())
                .parse()
                .map_err(|_| AppError::Internal("PORT must be a valid u16".into()))?,
            log_level: std::env::var("RUST_LOG").unwrap_or_else(|_| "info".to_string()),
            environment: std::env::var("ENVIRONMENT").unwrap_or_else(|_| "development".to_string()),
        })
    }

    pub fn is_production(&self) -> bool {
        self.environment == "production"
    }
}
