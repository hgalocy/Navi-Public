"""GLSL shaders for Navi's face renderer."""

kVertexShader = """
        #version 330
        in vec2 in_pos;
        void main() {
            gl_Position = vec4(in_pos, 0.0, 1.0);
        }
    """

kFragmentShader = """
        #version 330
        uniform vec2 resolution_;
        uniform vec2 gaze_;
        uniform float head_tilt_;
        uniform float blink_;
        uniform float mouth_open_;
        uniform float zoom_; 
        uniform float time_;
        
        out vec4 frag_color_;

        // Draw an ellipse
        float SdEllipipse(vec2 p, vec2 r) {
            float k = length(p / r);
            return k - 1.0;
        }

        void main() {
            vec2 uv = (gl_FragCoord.xy - 0.5 * resolution_) / resolution_.y;
            uv *= zoom_;

            // -------------------------
            // Whole Head Movement
            // -------------------------
            // subtle movement of whole face
            uv -= gaze_ * 0.015;
            // head rotation
            float s = sin(head_tilt_);
            float c = cos(head_tilt_);
            uv = mat2(c,-s,s,c) * uv;

            // -------------------------
            // Eyes
            // -------------------------
            vec3 base_cyan = vec3(0.4, 1.0, 1.0);
            float eye_offset = 0.33;
            float eye_y = 0.27;
            float eye_width = 0.23;
            float eye_height = 0.18;
            float dynamic_height = max(eye_height * (1.0 - blink_), 0.01);
            // move eye centers (gaze)
            vec2 left_center  = vec2(-eye_offset, eye_y);
            vec2 right_center = vec2( eye_offset, eye_y);
            vec2 left_uv  = uv - left_center;
            vec2 right_uv = uv - right_center;
            vec2 pupil_offset = gaze_ * 0.15;
            pupil_offset = clamp(pupil_offset, vec2(-0.04,-0.02), vec2(0.04,0.02));
            vec2 left_pupil_uv  = uv - (left_center  + pupil_offset);
            vec2 right_pupil_uv = uv - (right_center + pupil_offset);
            float left_eye  = length(vec2(left_uv.x / eye_width,  left_uv.y / dynamic_height));
            float right_eye = length(vec2(right_uv.x / eye_width, right_uv.y / dynamic_height));
            float eye_mask = max(
                smoothstep(1.0, 0.88, left_eye),
                smoothstep(1.0, 0.88, right_eye)
            );
            // brighter eye core
            float left_pupil  = length(vec2(left_pupil_uv.x / (eye_width*0.7), left_pupil_uv.y / (eye_height*0.7)));
            float right_pupil = length(vec2(right_pupil_uv.x / (eye_width*0.7), right_pupil_uv.y / (eye_height*0.7)));
            float core_glow = max(
                smoothstep(0.65, 0.2, left_pupil),
                smoothstep(0.65, 0.2, right_pupil)
            );
            // rainbow cheeky arc (top tint)
            float arc = clamp(1.0 - uv.y * 2.5, 0.0, 1.0);
            vec3 rainbow_tint = vec3(0.8, 0.9, 1.0);
            vec3 eye_color = mix(base_cyan, rainbow_tint, arc * 0.4);
            vec3 pupil_color = vec3(0.8, 1.0, 1.0);
            eye_color += pupil_color * core_glow * 0.9;
            float highlight = max(
                smoothstep(0.25, 0.0, left_pupil),
                smoothstep(0.25, 0.0, right_pupil)
            );
            eye_color += vec3(1.0, 1.0, 1.0) * highlight * 0.35;
            vec3 final_color = eye_color * eye_mask;

            // -------------------------
            // Blush
            // -------------------------
            vec2 blush_r = vec2(0.18, 0.07); // horizontal stretch oval
            float blush_y = eye_y - 0.25;
            // move blush outward to cheeks
            vec2 blush_left_center  = vec2(-eye_offset - 0.18, blush_y);
            vec2 blush_right_center = vec2( eye_offset + 0.18, blush_y);
            float bl = SdEllipipse(uv - blush_left_center, blush_r);
            float br = SdEllipipse(uv - blush_right_center, blush_r);
            float blush_mask = max(
                smoothstep(0.20, -0.10, bl),
                smoothstep(0.20, -0.10, br)
            );
            // keep blush subtle and soft
            vec3 blush_col = vec3(1.0, 0.25, 0.60);
            final_color += blush_col * blush_mask * 0.22;

            // -------------------------
            // Mouth
            //--------------------------
            vec2 mouth_center = vec2(0.0, -0.12);
            mouth_center.y += sin(time_ * 2.0) * 0.002;
            vec2 p = uv - mouth_center;
            // subtle organic mouth wobble when talking
            p.y += sin(p.x * 10.0) * mouth_open_ * 0.01;
            // width limit
            float half_width = 0.16;
            float width_mask = 1.0 - smoothstep(half_width - 0.015, half_width, abs(p.x));
            // smile curve (top boundary)
            float top_curve = p.x * p.x * mix(1.2, 1.5, mouth_open_);
            top_curve -= mouth_open_ * 0.015;
            // closed smile line
            float closed_smile =
                smoothstep(0.022, 0.0, abs(p.y - top_curve))
                * width_mask
                * (1.0 - smoothstep(0.02, 0.07, mouth_open_));
            // mouth opening depth
            float depth = max(mouth_open_ * 0.13, 0.0001);
            // ellipse parameters
            float a = 0.16;      // width
            float b = depth;     // height
            // ellipse centered under the smile
            float ellipse =
                (p.x*p.x)/(a*a) +
                ((p.y - (top_curve - depth))*(p.y - (top_curve - depth)))/(b*b);
            // inside ellipse
            float ellipse_edge = smoothstep(1.10, 0.98, ellipse);
            float inside_ellipse = smoothstep(1.12, 0.90, ellipse);
            // only draw bottom half
            inside_ellipse *= step(p.y, top_curve);
            // only when talking
            inside_ellipse *= smoothstep(0.02, 0.06, mouth_open_);
            // distance from the smile curve (used for glow)
            float dist_from_curve = abs(p.y - top_curve);
            // main glow like the idle smile
            float top_glow = smoothstep(0.14, 0.0, dist_from_curve);
            top_glow *= mix(1.0, 1.4, smoothstep(0.02, 0.07, mouth_open_));
            float body_fill = inside_ellipse * 0.25;
            float edge_glow = ellipse_edge * 0.75;
            float glow =
                top_glow * closed_smile +     // fuzzy smile edge
                edge_glow * inside_ellipse +  // fuzzy ellipse edge
                body_fill * inside_ellipse;   // soft interior
            // clamp so it doesn't over-brighten
            glow = clamp(glow, 0.0, 1.0);
            vec3 mouth_color = vec3(0.4, 1.0, 1.0);
            final_color += mouth_color * glow * 0.9;
            
            frag_color_ = vec4(final_color, 1.0);
        }"""
