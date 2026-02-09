use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TaskStatus {
    Pending,
    InProgress,
    Completed,
    HasSubtasks,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkType {
    Focus,
    Creative,
    Social,
    Independent,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EnergyLevel {
    High,
    Medium,
    Low,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RewardTriggerType {
    Initiation,
    FirstStep,
    Resume,
    Completion,
    Streak,
    Milestone,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Task {
    pub id: Uuid,
    pub title: String,
    pub status: TaskStatus,
    pub work_type: WorkType,
    pub urgency: u8,
    pub time_estimate_minutes: u32,
    pub energy_required: EnergyLevel,
    pub created_at: DateTime<Utc>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    pub rejection_count: u32,
    pub rejection_notes: Vec<String>,
    pub ai_context: Option<String>,
    pub inline_steps: Option<String>,
    pub parent_task_id: Option<Uuid>,
    pub sequence: Option<u32>,
    pub progress_notes: Vec<String>,
    pub steps_completed: u32,
    pub resume_count: u32,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_status_serializes_to_snake_case() {
        let json = serde_json::to_string(&TaskStatus::InProgress).unwrap();
        assert_eq!(json, "\"in_progress\"");
    }

    #[test]
    fn task_status_deserializes_from_snake_case() {
        let status: TaskStatus = serde_json::from_str("\"has_subtasks\"").unwrap();
        assert_eq!(status, TaskStatus::HasSubtasks);
    }

    #[test]
    fn work_type_round_trips() {
        for wt in [
            WorkType::Focus,
            WorkType::Creative,
            WorkType::Social,
            WorkType::Independent,
        ] {
            let json = serde_json::to_string(&wt).unwrap();
            let deserialized: WorkType = serde_json::from_str(&json).unwrap();
            assert_eq!(wt, deserialized);
        }
    }

    #[test]
    fn energy_level_round_trips() {
        for el in [EnergyLevel::High, EnergyLevel::Medium, EnergyLevel::Low] {
            let json = serde_json::to_string(&el).unwrap();
            let deserialized: EnergyLevel = serde_json::from_str(&json).unwrap();
            assert_eq!(el, deserialized);
        }
    }

    #[test]
    fn reward_trigger_type_serializes_correctly() {
        let json = serde_json::to_string(&RewardTriggerType::Initiation).unwrap();
        assert_eq!(json, "\"initiation\"");
        let json = serde_json::to_string(&RewardTriggerType::FirstStep).unwrap();
        assert_eq!(json, "\"first_step\"");
    }
}
