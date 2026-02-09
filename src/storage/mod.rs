pub mod memory;

use crate::domain::task::Task;
use crate::error::AppError;
use uuid::Uuid;

pub trait TaskRepository: Send + Sync {
    fn create(&self, task: Task) -> Result<Task, AppError>;
    fn get(&self, id: Uuid) -> Result<Option<Task>, AppError>;
    fn list_pending(&self) -> Result<Vec<Task>, AppError>;
    fn update(&self, task: Task) -> Result<Task, AppError>;
}
