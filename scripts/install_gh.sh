# install_gh.sh -- developer convenience script, NOT part of the pipeline.
#
# Installs GitHub's official command-line tool, `gh` (used by
# get_comments.sh and by Claude Code itself to read/create pull requests),
# on a Debian/Ubuntu-style Linux system such as WSL. This is the standard
# install recipe published by GitHub itself -- it does not touch this
# project's own code or data in any way.
#
# Step 1: Make sure `wget` (a tool for downloading files from the internet)
# is available, installing it first if it's missing.
type -p wget >/dev/null || (sudo apt update && sudo DEBIAN_FRONTEND=noninteractive apt install wget -y)

# Step 2: Create the folder where trusted download signing keys are kept.
sudo mkdir -p -m 755 /etc/apt/keyrings

# Step 3: Download GitHub's official signing key and save it there. This
# key lets the system PROVE that the `gh` software it downloads next really
# did come from GitHub and wasn't tampered with in transit.
wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg

# Step 4: Tell the system's package manager (`apt`, the tool Ubuntu/Debian
# use to install software) that GitHub's own software repository exists and
# is trusted via the key saved above.
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null

# Step 5: Refresh the package manager's list of available software (now
# including GitHub's newly-added repository), then install `gh` itself.
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install gh -y
