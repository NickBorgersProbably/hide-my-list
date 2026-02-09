use std::collections::HashMap;
use std::sync::RwLock;
use uuid::Uuid;

use crate::domain::task::{Task, TaskStatus};
use crate::error::AppError;
use crate::storage::TaskRepository;

pub struct InMemoryTaskRepository {
    tasks: RwLock<HashMap<Uuid, Task>>,
}

impl InMemoryTaskRepository {
    pub fn new() -> Self {
        Self {
            tasks: RwLock::new(HashMap::new()),
        }
    }
}

impl Default for InMemoryTaskRepository {
    fn default() -> Self {
        Self::new()
    }
}

impl TaskRepository for InMemoryTaskRepository {
    fn create(&self, task: Task) -> Result<Task, AppError> {
        let mut tasks = self
            .tasks
            .write()
            .map_err(|e| AppError::Internal(format!("lock poisoned: {e}")))?;
        tasks.insert(task.id, task.clone());
        Ok(task)
    }

    fn get(&self, id: Uuid) -> Result<Option<Task>, AppError> {
        let tasks = self
            .tasks
            .read()
            .map_err(|e| AppError::Internal(format!("lock poisoned: {e}")))?;
        Ok(tasks.get(&id).cloned())
    }

    fn list_pending(&self) -> Result<Vec<Task>, AppError> {
        let tasks = self
            .tasks
            .read()
            .map_err(|e| AppError::Internal(format!("lock poisoned: {e}")))?;
        Ok(tasks
            .values()
            .filter(|t| t.status == TaskStatus::Pending)
            .cloned()
            .collect())
    }

    fn update(&self, task: Task) -> Result<Task, AppError> {
        let mut tasks = self
            .tasks
            .write()
            .map_err(|e| AppError::Internal(format!("lock poisoned: {e}")))?;
        if !tasks.contains_key(&task.id) {
            return Err(AppError::NotFound(format!("task {} not found", task.id)));
        }
        tasks.insert(task.id, task.clone());
        Ok(task)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::task::{EnergyLevel, WorkType};
    use chrono::Utc;

    fn make_task(title: &str) -> Task {
        Task {
            id: Uuid::new_v4(),
            title: title.to_string(),
            status: TaskStatus::Pending,
            work_type: WorkType::Focus,
            urgency: 50,
            time_estimate_minutes: 30,
            energy_required: EnergyLevel::Medium,
            created_at: Utc::now(),
            started_at: None,
            completed_at: None,
            rejection_count: 0,
            rejection_notes: vec![],
            ai_context: None,
            inline_steps: None,
            parent_task_id: None,
            sequence: None,
            progress_notes: vec![],
            steps_completed: 0,
            resume_count: 0,
        }
    }

    #[test]
    fn create_and_get_task() {
        let repo = InMemoryTaskRepository::new();
        let task = make_task("Test task");
        let id = task.id;
        repo.create(task).unwrap();
        let fetched = repo.get(id).unwrap().unwrap();
        assert_eq!(fetched.title, "Test task");
    }

    #[test]
    fn list_pending_filters_correctly() {
        let repo = InMemoryTaskRepository::new();
        let pending = make_task("Pending task");
        let mut completed = make_task("Completed task");
        completed.status = TaskStatus::Completed;

        repo.create(pending).unwrap();
        repo.create(completed).unwrap();

        let results = repo.list_pending().unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].title, "Pending task");
    }

    #[test]
    fn update_nonexistent_task_returns_not_found() {
        let repo = InMemoryTaskRepository::new();
        let task = make_task("Ghost");
        let result = repo.update(task);
        assert!(matches!(result, Err(AppError::NotFound(_))));
    }
}
