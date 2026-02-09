use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Mood {
    Focused,
    Creative,
    Social,
    Tired,
    Stressed,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserPreference {
    pub user_id: String,
    pub preferred_beverage: Option<String>,
    pub comfort_spot: Option<String>,
    pub transition_ritual: Option<String>,
    pub focus_music: Option<String>,
    pub break_activity: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mood_round_trips() {
        for mood in [
            Mood::Focused,
            Mood::Creative,
            Mood::Social,
            Mood::Tired,
            Mood::Stressed,
        ] {
            let json = serde_json::to_string(&mood).unwrap();
            let deserialized: Mood = serde_json::from_str(&json).unwrap();
            assert_eq!(mood, deserialized);
        }
    }
}
