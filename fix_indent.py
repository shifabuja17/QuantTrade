with open("execution.py", "r") as f:
    content = f.read()
content = content.replace('state_data = {', '        state_data = {', 1)
with open("execution.py", "w") as f:
    f.write(content)
