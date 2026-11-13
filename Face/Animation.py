"""Render Navi's idle and speaking face animations."""

from Face import Shaders

import pyglet
import moderngl
import numpy as np
import time
import math

kBlinkDuration = 0.16

class AnimState:
    def __init__(self):
        # ---- Calculations made for mouth movement on speaking ----
        self.current_mouth_ = 0.0
        self.talk_active_ = False

        # gaze
        self.next_gaze_time_ = 0.0
        self.target_gaze_ = [0.0, 0.0]
        self.current_gaze_ = [0.0, 0.0]

        # blink
        self.next_blink_time_ = np.random.uniform(3, 7)
        self.blink_start_ = -1.0

        # head
        self.target_tilt_ = 0
        self.current_tilt_ = 0
        self.next_head_move_ = 0

def run(*, submit_command, on_tick, playback_sample):
    """Render IPC speech envelopes and send window keyboard input to Coordinator."""
    state = AnimState()
    window_ = pyglet.window.Window(fullscreen=True)
    width_, height_ = window_.get_framebuffer_size()
    ctx_ = moderngl.create_context()

    prog_ = ctx_.program(
        vertex_shader= Shaders.kVertexShader,
        fragment_shader= Shaders.kFragmentShader,
    )

    prog_["resolution_"].value = (width_, height_)
    prog_["zoom_"].value = 1.35
    prog_["gaze_"].value = (0.0, 0.0)
    prog_["head_tilt_"].value = 0.0
    prog_["blink_"].value = 0.0
    prog_["mouth_open_"].value = 0.0
    prog_["time_"].value = 0.0

    def SyncFramebufferSize():
        nonlocal width_, height_

        fb_width, fb_height = window_.get_framebuffer_size()
        if fb_width <= 0 or fb_height <= 0:
            return

        width_, height_ = fb_width, fb_height
        ctx_.viewport = (0, 0, width_, height_)
        prog_["resolution_"].value = (width_, height_)

    SyncFramebufferSize()

    start_time_ = time.time()

    quad_ = np.array([
        -1.0, -1.0,
        1.0, -1.0,
        -1.0,  1.0,
        1.0,  1.0,
    ], dtype="f4")

    vbo_ = ctx_.buffer(quad_.tobytes())
    vao_ = ctx_.simple_vertex_array(prog_, vbo_, "in_pos")

    @window_.event
    def on_resize(w, h):
        SyncFramebufferSize()

    #Make Navi look like she's looking around
    def MicroJitter(t, speed=10.0, amount=0.004, offset=0.0):
        return math.sin(t * speed + offset) * amount

    def UpdateGaze(t):
        if t > state.next_gaze_time_:
            state.target_gaze_ = [
                np.random.uniform(-0.35, 0.35),
                np.random.uniform(-0.15, 0.15)
            ]
            state.next_gaze_time_ = t + np.random.uniform(1.5, 4.0)
        # smooth eye motion
        speed = 0.08
        state.current_gaze_[0] += (state.target_gaze_[0]-state.current_gaze_[0]) * speed
        state.current_gaze_[1] += (state.target_gaze_[1]-state.current_gaze_[1]) * speed

        jitter_x = MicroJitter(t, 11.3, 0.002)
        jitter_y = MicroJitter(t, 9.7, 0.002, 1.7)
        return [state.current_gaze_[0] + jitter_x, state.current_gaze_[1] + jitter_y]

    #Make Navi blink every so often
    def UpdateBlink(t):
        # start blink
        if state.blink_start_ < 0 and t > state.next_blink_time_:
            state.blink_start_ = t
        # if blinking
        if state.blink_start_ >= 0:
            progress = (t - state.blink_start_) / kBlinkDuration
            if progress >= 1.0:
                state.blink_start_ = -1.0
                state.next_blink_time_ = t + np.random.uniform(3,7)
                return 0.0
            return math.sin(progress * math.pi) ** 0.6
        return 0.0

    #Head bobbing swaying slightly
    def UpdateHead(t):
        if t > state.next_head_move_:
            state.target_tilt_ = np.random.uniform(-0.07, 0.07)
            state.next_head_move_ = t + np.random.uniform(3, 6)
        state.current_tilt_ += (state.target_tilt_ - state.current_tilt_) * 0.02
        return state.current_tilt_ + MicroJitter(t, 6.5, 0.002)

    typed = []

    @window_.event
    def on_text(text):
        typed.extend(char for char in text if char.isprintable())
        del typed[4000:]

    @window_.event
    def on_key_press(symbol, modifiers):
        if symbol == pyglet.window.key.ENTER:
            command = "".join(typed).strip()
            typed.clear()
            if command:
                submit_command(command)
        elif symbol == pyglet.window.key.BACKSPACE and typed:
            typed.pop()

    @window_.event
    def on_draw():
        on_tick()
        SyncFramebufferSize()

        ctx_.clear(0.0, 0.0, 0.0)
        t = time.time() - start_time_
        prog_["time_"].value = t

        env_play, still_playing = playback_sample()
        state.talk_active_ = still_playing
        if state.talk_active_:
            target_mouth = env_play
            prog_["gaze_"].value = (0.0, 0.0)
            prog_["head_tilt_"].value = UpdateHead(t) + target_mouth * 0.01
        else:
            prog_["gaze_"].value = UpdateGaze(t)
            prog_["head_tilt_"].value = UpdateHead(t)
            target_mouth = 0.0


        # smooth transition
        state.current_mouth_ += (target_mouth - state.current_mouth_) * 0.25
        prog_["mouth_open_"].value = state.current_mouth_

        # ---- Subtle breathing motion ----
        prog_["zoom_"].value = 1.35 + math.sin(t * 0.25) * 0.01

        # ---- Blinking ----
        prog_["blink_"].value = UpdateBlink(t)

        vao_.render(moderngl.TRIANGLE_STRIP)

    pyglet.app.run()
