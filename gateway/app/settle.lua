-- Close a hold with the actual cost (state "settled") or zero ("released": the model call never
-- happened or was refused unbilled). Idempotent: closing a closed hold changes nothing. A late
-- settle of an expired_charged hold still corrects it to the real cost.
-- KEYS[1] team spent  KEYS[2] user spent  KEYS[3] hold record  KEYS[4] deadline index
-- ARGV[1] actual  ARGV[2] terminal state  ARGV[3] now  ARGV[4] request id  ARGV[5] record ttl
-- Returns {applied, delta, team_spent, user_spent}
local s = redis.call('HGET', KEYS[3], 'state')
local applied, delta = 0, 0
if s == 'reserved' or s == 'invoking' or s == 'expired_charged' then
  delta = tonumber(ARGV[1]) - tonumber(redis.call('HGET', KEYS[3], 'amount'))
  if delta ~= 0 then
    redis.call('INCRBY', KEYS[1], delta)
    redis.call('INCRBY', KEYS[2], delta)
  end
  redis.call('HSET', KEYS[3], 'state', ARGV[2], 'actual', ARGV[1], 'closed_at', ARGV[3])
  redis.call('EXPIRE', KEYS[3], ARGV[5])
  redis.call('ZREM', KEYS[4], ARGV[4])
  applied = 1
end
return {applied, delta, tonumber(redis.call('GET', KEYS[1]) or '0'), tonumber(redis.call('GET', KEYS[2]) or '0')}
