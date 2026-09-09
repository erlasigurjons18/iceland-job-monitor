# Iceland Job Monitor

Checks these career pages every day at 17:00 Atlantic/Reykjavik:

- Eirr Medical
- Nox Medical
- Sidekick Health

Only jobs whose rendered listing indicates Iceland (including common Icelandic city names) are considered.
The evergreen Nox Medical "General Application" is ignored.

## Email setup

The defaults use Gmail SMTP. In GitHub, open:

Settings -> Secrets and variables -> Actions -> New repository secret

Add:

- `SMTP_USERNAME`: your Gmail address
- `SMTP_PASSWORD`: a Gmail App Password (not your normal Google password)
- `EMAIL_TO`: the address that should receive alerts

Optional for a non-Gmail SMTP provider:

- `SMTP_HOST`
- `SMTP_PORT`
- `EMAIL_FROM`

## Test it

Open the repository's **Actions** tab, choose **Iceland job monitor**, then select **Run workflow**.

On the first successful run, every currently listed Iceland vacancy is considered new and will send an email.
After an alert is sent, `job_monitor_state.json` is committed automatically to the repository.

The same normalized company + position title is suppressed for 30 days. After 30 days it becomes eligible to alert again if it is still/reappears on the careers page.
