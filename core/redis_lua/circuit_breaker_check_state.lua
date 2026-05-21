local state = redis.call("get", KEYS[1])
if not state or state == false then
    return "closed"
end
if state == "open" then
    local open_until = redis.call("get", KEYS[2])
    if open_until and tonumber(ARGV[1]) >= tonumber(open_until) then
        redis.call("set", KEYS[1], "half_open")
        return "half_open"
    end
end
return state
