# Copy to deploy.config.psd1 (git ignores that name) and fill in. Read by deploy.ps1.
@{
    # How to reach the server: user@host, or a Host alias from ~\.ssh\config.
    SshTarget    = 'topman@your-server.example.com'
    SshPort      = 22
    # Private key to use. Leave empty to use ssh-agent or your ~\.ssh\config.
    SshKey       = ''

    # Public address nginx serves the site on. Used for the API's allowed origins when the
    # settings file is first written, and for a health check through nginx after each deploy.
    # Leave empty to skip both.
    PublicUrl    = 'https://your-domain.example.com'

    # Server layout. The defaults match the README.
    BaseDir      = '/opt/solar-battery-planner'
    AppUser      = 'solar'
    ServiceName  = 'solar-battery-planner'
    DatabaseName = 'solar'
    Port         = 8000
    Workers      = 2
    KeepReleases = 5

    # Run the nightly auto-apply pass from cron every five minutes (README step 5).
    # Only users who opted in to inverter control are affected.
    NightlyCron  = $false

    # Python used for the local backend tests. Empty: backend\.venv, then py or python.
    LocalPython  = ''
}
