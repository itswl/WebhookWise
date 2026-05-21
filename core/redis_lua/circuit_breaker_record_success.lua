local state = redis.call("get", KEYS[2])
if state == "half_open" or state == "open" then
    redis.call("del", KEYS[1])
    redis.call("set", KEYS[2], "closed")
    redis.call("del", KEYS[3])
end
return 0
