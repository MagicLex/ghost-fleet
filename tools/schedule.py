"""Schedule a Hopsworks job: python3 tools/schedule.py <job> <quartz-cron> [--run]"""
import sys

import hopsworks


def main():
    name, cron = sys.argv[1], sys.argv[2]
    job = hopsworks.login().get_job_api().get_job(name)
    job.schedule(cron_expression=cron)
    print(f"{name} scheduled: {cron}")
    if "--run" in sys.argv:
        ex = job.run(await_termination=False)
        print(f"execution {ex.id} launched")


if __name__ == "__main__":
    main()
