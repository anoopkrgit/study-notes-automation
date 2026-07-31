# get_comments.sh -- developer convenience script, NOT part of the pipeline.
#
# Prints every comment left on GitHub pull request #1 of this project, so a
# developer can read PR feedback from the terminal instead of opening a web
# browser. It has nothing to do with study notes, chapters, or the AI
# pipeline -- it only talks to GitHub.
#
# Step 1: Borrow the GitHub login token that's already saved on this
# computer (via `git credential fill`, the same mechanism `git push` uses to
# avoid asking for a password every time) and hand it to `gh`, GitHub's
# command-line tool, through the GH_TOKEN variable it expects.
export GH_TOKEN=$(echo -e 'protocol=https\nhost=github.com\n' | git credential fill | grep password= | cut -d= -f2)

# Step 2: Move into this project's folder, since `gh pr view` needs to be
# run from inside a git repository to know which GitHub project to ask.
cd /mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned

# Step 3: Ask GitHub for pull request #1 and print its comments.
gh pr view 1 --comments
