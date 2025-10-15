## Docker setup for local MySQL

1. Copy sample env and fill secrets (DO NOT COMMIT mysql.env):

   cp mysql.env.sample mysql.env
   # then edit mysql.env to set real passwords

2. Create host data directory (Windows PowerShell):

   mkdir E:\photonics\mysql-data

3. Start the container with Docker Compose (project root):

   docker compose up -d

4. Check logs:

   docker compose logs -f mysql

Notes
- This compose file binds the host path E:/photonics/mysql-data to /var/lib/mysql inside the container.
- If you want Docker to manage the volume instead, change the "volumes" section to use a named volume.
- Keep mysql.env out of version control.
