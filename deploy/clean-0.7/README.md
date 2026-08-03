# Clean Omnigent 0.7 Pi deployment

Run the installer from the repository root:

```bash
sudo bash deploy/clean-0.7/install.sh
```

Verify the deployment:

```bash
curl --fail http://127.0.0.1:4097/health
curl --fail http://127.0.0.1:4097/v1/hosts
systemctl status omnigent.service omnigent-host.service
```
