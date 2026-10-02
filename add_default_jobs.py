from scheduler import SchedulerStore
import json

def register_daily_summary():
    store = SchedulerStore()
    
    # Check if job already exists
    jobs = store.list_jobs()
    for job in jobs:
        if job["name"] == "daily_summary" and job["job_type"] == "maintenance":
            print(f"Job 'daily_summary' already exists with ID: {job['id']}")
            return job['id']

    job_id = store.add_job(
        name="daily_summary",
        cron_expression="0 0 * * *", # Run daily at midnight UTC
        job_type="maintenance",
        job_payload={"task": "daily_summary"},
        description="Daily system summary generation."
    )
    print(f"Registered daily_summary job with ID: {job_id}")
    return job_id

if __name__ == '__main__':
    register_daily_summary()