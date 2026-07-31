export GH_TOKEN=$(echo -e 'protocol=https\nhost=github.com\n' | git credential fill | grep password= | cut -d= -f2)
cd /mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned
gh pr view 1 --comments
