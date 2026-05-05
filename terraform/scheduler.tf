# EventBridge Scheduler schedule group used by CursiveDebouncer.
# Individual schedules are created on the fly by the Debouncer Lambda
# (one per active letterId, with ActionAfterCompletion=DELETE) — they are
# NOT TF-managed because they are ephemeral runtime state.
resource "aws_scheduler_schedule_group" "debounce" {
  name = "cursive-debounce"
}
