#!/usr/bin/env python3
import gpiod
import time

# Pin definitions
PINS = {
    'PADDLE_UP':   ('gpiochip2', 23),
    'PADDLE_DOWN': ('gpiochip2', 25),
    'BALL_BIT0':   ('gpiochip0', 26),
    'BALL_BIT1':   ('gpiochip0', 23),
    'BALL_BIT2':   ('gpiochip0', 27),
}

ZONES = 8
HISTORY_SIZE = 8

# State tracking
paddle_position = 4.0
ball_history = []
target_position = 4.0
ball_x_history = []
ball_x_estimate = 0.5
frames_since_direction_change = 0
last_action = "HOLD"
frames_in_same_direction = 0

def setup_gpio():
    global paddle_up, paddle_down, ball_bit0, ball_bit1, ball_bit2
    global chip0, chip2
    
    chip0 = gpiod.Chip('gpiochip0')
    chip2 = gpiod.Chip('gpiochip2')
    
    paddle_up = chip2.get_line(PINS['PADDLE_UP'][1])
    paddle_down = chip2.get_line(PINS['PADDLE_DOWN'][1])
    
    paddle_up.request(consumer="pong_ai", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
    paddle_down.request(consumer="pong_ai", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[0])
    
    ball_bit0 = chip0.get_line(PINS['BALL_BIT0'][1])
    ball_bit1 = chip0.get_line(PINS['BALL_BIT1'][1])
    ball_bit2 = chip0.get_line(PINS['BALL_BIT2'][1])
    
    ball_bit0.request(consumer="pong_ai", type=gpiod.LINE_REQ_DIR_IN)
    ball_bit1.request(consumer="pong_ai", type=gpiod.LINE_REQ_DIR_IN)
    ball_bit2.request(consumer="pong_ai", type=gpiod.LINE_REQ_DIR_IN)

def read_ball_zone():
    """Read 3-bit ball position (0-7)"""
    bit0 = ball_bit0.get_value()
    bit1 = ball_bit1.get_value()
    bit2 = ball_bit2.get_value()
    zone = (bit2 << 2) | (bit1 << 1) | bit0
    return min(zone, 7)

def update_ball_tracking(ball_zone):
    """Add new position to history"""
    global ball_history
    ball_history.append(ball_zone)
    if len(ball_history) > HISTORY_SIZE:
        ball_history.pop(0)

def calculate_ball_velocity():
    """Get average velocity from recent samples"""
    if len(ball_history) < 2:
        return 0.0
    
    recent = ball_history[-3:] if len(ball_history) >= 3 else ball_history
    if len(recent) < 2:
        return 0.0
    
    total_change = recent[-1] - recent[0]
    frames = len(recent) - 1
    return total_change / frames if frames > 0 else 0.0

def estimate_ball_x_position():
    """
    Estimate X pos (0=player, 1=AI) from travel time.
    Ball moves diagonally at constant angle, so we track frames since velocity changed.
    """
    global ball_x_estimate, frames_since_direction_change
    
    if len(ball_history) < 3:
        return 0.5
    
    current_vel = ball_history[-1] - ball_history[-2] if len(ball_history) >= 2 else 0
    prev_vel = ball_history[-2] - ball_history[-3] if len(ball_history) >= 3 else 0
    
    # Velocity reversed = hit a paddle
    if len(ball_history) >= 3:
        if abs(current_vel) > 0.2 and abs(prev_vel) > 0.2:
            if current_vel * prev_vel < 0:
                frames_since_direction_change = 0
                if ball_x_estimate > 0.5:
                    ball_x_estimate = 0.95
                else:
                    ball_x_estimate = 0.05
    
    if abs(current_vel) > 0.1:
        frames_since_direction_change += 1
    
    # ~30 frames to cross screen
    FRAMES_TO_CROSS = 30.0
    travel_progress = min(frames_since_direction_change / FRAMES_TO_CROSS, 1.0)
    
    if ball_x_estimate > 0.5:
        ball_x_estimate = 0.95 - (travel_progress * 0.95)
    else:
        ball_x_estimate = 0.05 + (travel_progress * 0.95)
    
    return ball_x_estimate

def is_ball_approaching():
    """Only track when ball is in right half"""
    x_pos = estimate_ball_x_position()
    return x_pos > 0.5

def predict_ball_position(current_zone, velocity):
    """Predict pos 10 frames ahead with wall bounces"""
    if abs(velocity) < 0.1:
        return current_zone
    
    predicted = current_zone + (velocity * 10)
    
    # Bounce off walls
    while predicted < 0 or predicted >= ZONES:
        if predicted < 0:
            predicted = -predicted
        elif predicted >= ZONES:
            predicted = (ZONES - 1) - (predicted - (ZONES - 1))
    
    return max(0, min(ZONES - 1, predicted))

def calculate_target_position(ball_zone):
    """Where paddle should go"""
    velocity = calculate_ball_velocity()
    
    if abs(velocity) > 0.1:
        target = predict_ball_position(ball_zone, velocity)
    else:
        target = ball_zone
    
    return target

def smooth_ai_move(target, current_paddle, ball_approaching):
    """
    Smooth movement with hysteresis to prevent jitter.
    Requires 3 frames before changing direction.
    """
    global last_action, frames_in_same_direction
    
    if not ball_approaching:
        target = 4.0
    
    difference = target - current_paddle
    
    DEAD_ZONE = 0.8
    if abs(difference) < DEAD_ZONE:
        if abs(difference) < 0.3:
            last_action = "HOLD"
            frames_in_same_direction = 0
            return "HOLD", 0.0
    
    desired_action = "UP" if difference < 0 else "DOWN"
    
    # Hysteresis: need 3 frames to switch directions
    if desired_action != last_action and last_action != "HOLD":
        if frames_in_same_direction < 3:
            frames_in_same_direction += 1
            return last_action, 0.18
    
    if desired_action == last_action:
        frames_in_same_direction += 1
    else:
        frames_in_same_direction = 0
    
    last_action = desired_action
    return desired_action, 0.18

def execute_move(action, speed):
    """Set GPIO and update position"""
    global paddle_position
    
    if action == "UP":
        paddle_up.set_value(1)
        paddle_down.set_value(0)
        paddle_position = max(0, paddle_position - speed)
    elif action == "DOWN":
        paddle_up.set_value(0)
        paddle_down.set_value(1)
        paddle_position = min(ZONES - 1, paddle_position + speed)
    else:
        paddle_up.set_value(0)
        paddle_down.set_value(0)

def cleanup():
    try:
        paddle_up.set_value(0)
        paddle_down.set_value(0)
        paddle_up.release()
        paddle_down.release()
        ball_bit0.release()
        ball_bit1.release()
        ball_bit2.release()
        chip0.close()
        chip2.close()
    except:
        pass

def main():
    setup_gpio()
    
    frame_count = 0
    
    try:
        while True:
            current_time = time.time()
            
            ball_zone = read_ball_zone()
            update_ball_tracking(ball_zone)
            
            ball_approaching = is_ball_approaching()
            
            if ball_approaching:
                target = calculate_target_position(ball_zone)
            else:
                target = 4.0
            
            action, move_speed = smooth_ai_move(target, paddle_position, ball_approaching)
            execute_move(action, move_speed)
            
            frame_count += 1
            time.sleep(0.05)
            
    except KeyboardInterrupt:
        cleanup()
    except Exception as e:
        cleanup()

if __name__ == "__main__":
    main()
