// N3D Scanner - step/dir controller over serial
// Assumes Arduino UNO + CNC Shield V3 pinout (X/Y/Z/A).

const int ENABLE_PIN = 8; // Active-low on CNC Shield V3

const unsigned int default_step_delay_us = 800;
unsigned int step_delay_us = default_step_delay_us;
const unsigned int step_pulse_us = 10;
unsigned int ramp_steps_limit = 50;
unsigned int ramp_extra_delay_us = 4000;

struct AxisConfig {
  char name;
  int step_pin;
  int dir_pin;
  float steps_per_mm;
  float min_mm;
  float max_mm;
  long position_steps;
};

AxisConfig axes[] = {
  {'X', 2, 5, 80.0f, -1000.0f, 1000.0f, 0},
  {'Y', 3, 6, 80.0f, -1000.0f, 1000.0f, 0},
  {'Z', 4, 7, 80.0f, -1000.0f, 1000.0f, 0},
  {'A', 12, 13, 80.0f, -1000.0f, 1000.0f, 0}
};

const int axis_count = sizeof(axes) / sizeof(axes[0]);

void setup() {
  for (int i = 0; i < axis_count; i++) {
    pinMode(axes[i].step_pin, OUTPUT);
    pinMode(axes[i].dir_pin, OUTPUT);
  }
  pinMode(ENABLE_PIN, OUTPUT);

  digitalWrite(ENABLE_PIN, HIGH); // Disable by default

  Serial.begin(115200);
  Serial.println("N3D_READY");
}

int axis_index_from_name(String axis) {
  if (axis.length() != 1) {
    return -1;
  }
  char name = axis.charAt(0);
  for (int i = 0; i < axis_count; i++) {
    if (axes[i].name == name) {
      return i;
    }
  }
  return -1;
}

long mm_to_steps(AxisConfig &axis, float mm) {
  return (long)(mm * axis.steps_per_mm);
}

float steps_to_mm(AxisConfig &axis, long steps) {
  return (float)steps / axis.steps_per_mm;
}

unsigned int delay_for_step(unsigned long index, unsigned long total_steps, unsigned int base_delay_us) {
  if (total_steps <= 1 || ramp_steps_limit == 0 || ramp_extra_delay_us == 0) {
    return base_delay_us;
  }

  unsigned long ramp_steps = total_steps / 2;
  if (ramp_steps > ramp_steps_limit) {
    ramp_steps = ramp_steps_limit;
  }
  if (ramp_steps == 0) {
    return base_delay_us;
  }

  unsigned long from_end = total_steps - 1 - index;
  unsigned long edge = index < from_end ? index : from_end;
  if (edge >= ramp_steps) {
    return base_delay_us;
  }

  unsigned long extra = ((unsigned long)ramp_extra_delay_us * (ramp_steps - edge)) / ramp_steps;
  return base_delay_us + (unsigned int)extra;
}

void step_axis(AxisConfig &axis, long steps, unsigned int delay_us) {
  if (steps == 0) {
    return;
  }

  if (steps > 0) {
    digitalWrite(axis.dir_pin, HIGH);
  } else {
    digitalWrite(axis.dir_pin, LOW);
    steps = -steps;
  }
  delayMicroseconds(20);

  unsigned long total_steps = (unsigned long)steps;
  for (long i = 0; i < steps; i++) {
    digitalWrite(axis.step_pin, HIGH);
    delayMicroseconds(step_pulse_us);
    digitalWrite(axis.step_pin, LOW);
    delayMicroseconds(delay_for_step((unsigned long)i, total_steps, delay_us));
  }
}

bool clamp_steps_to_limits(AxisConfig &axis, long &steps) {
  long min_steps = mm_to_steps(axis, axis.min_mm);
  long max_steps = mm_to_steps(axis, axis.max_mm);
  long target_steps = axis.position_steps + steps;
  if (target_steps < min_steps) {
    steps = min_steps - axis.position_steps;
    return true;
  }
  if (target_steps > max_steps) {
    steps = max_steps - axis.position_steps;
    return true;
  }
  return false;
}

void handle_command(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }

  if (line.equalsIgnoreCase("ENABLE")) {
    digitalWrite(ENABLE_PIN, LOW);
    Serial.println("OK ENABLE");
    return;
  }

  if (line.equalsIgnoreCase("DISABLE")) {
    digitalWrite(ENABLE_PIN, HIGH);
    Serial.println("OK DISABLE");
    return;
  }

  if (line.startsWith("SPEED")) {
    int space = line.indexOf(' ');
    if (space > 0) {
      unsigned int val = (unsigned int)line.substring(space + 1).toInt();
      if (val > 0) {
        step_delay_us = val;
      }
      Serial.println("OK SPEED");
    } else {
      Serial.println("ERR SPEED");
    }
    return;
  }

  if (line.startsWith("RAMP")) {
    // Format: RAMP 50 4000  (ramp_steps extra_delay_us)
    int first = line.indexOf(' ');
    int second = line.indexOf(' ', first + 1);
    if (first < 0 || second < 0) {
      Serial.println("ERR RAMP");
      return;
    }

    long steps = line.substring(first + 1, second).toInt();
    long extra = line.substring(second + 1).toInt();
    if (steps < 0 || steps > 1000 || extra < 0 || extra > 30000) {
      Serial.println("ERR RAMP");
      return;
    }

    ramp_steps_limit = (unsigned int)steps;
    ramp_extra_delay_us = (unsigned int)extra;
    Serial.println("OK RAMP");
    return;
  }

  if (line.startsWith("MOVE_MM")) {
    // Format: MOVE_MM X 10.5 [delay_us]
    int first = line.indexOf(' ');
    int second = line.indexOf(' ', first + 1);
    int third = line.indexOf(' ', second + 1);
    if (first < 0 || second < 0) {
      Serial.println("ERR MOVE_MM");
      return;
    }

    String axis = line.substring(first + 1, second);
    int axis_idx = axis_index_from_name(axis);
    if (axis_idx < 0) {
      Serial.println("ERR AXIS");
      return;
    }

    float mm = 0.0f;
    if (third > 0) {
      mm = line.substring(second + 1, third).toFloat();
    } else {
      mm = line.substring(second + 1).toFloat();
    }

    unsigned int delay_us = step_delay_us;
    if (third > 0) {
      unsigned int parsed = (unsigned int)line.substring(third + 1).toInt();
      if (parsed > 0) {
        delay_us = parsed;
      }
    }

    AxisConfig &target = axes[axis_idx];
    long steps = mm_to_steps(target, mm);
    bool clamped = clamp_steps_to_limits(target, steps);
    if (steps == 0) {
      Serial.println(clamped ? "WARN LIMIT" : "OK MOVE_MM");
      return;
    }

    step_axis(target, steps, delay_us);
    target.position_steps += steps;
    Serial.println(clamped ? "WARN LIMIT" : "OK MOVE_MM");
    return;
  }

  if (line.startsWith("MOVE ")) {
    // Format: MOVE X 1000 [delay_us]  (steps)
    int first = line.indexOf(' ');
    int second = line.indexOf(' ', first + 1);
    int third = line.indexOf(' ', second + 1);
    if (first < 0 || second < 0) {
      Serial.println("ERR MOVE");
      return;
    }

    String axis = line.substring(first + 1, second);
    int axis_idx = axis_index_from_name(axis);
    if (axis_idx < 0) {
      Serial.println("ERR AXIS");
      return;
    }

    long steps = 0;
    if (third > 0) {
      steps = line.substring(second + 1, third).toInt();
    } else {
      steps = line.substring(second + 1).toInt();
    }

    unsigned int delay_us = step_delay_us;
    if (third > 0) {
      unsigned int parsed = (unsigned int)line.substring(third + 1).toInt();
      if (parsed > 0) {
        delay_us = parsed;
      }
    }

    AxisConfig &target = axes[axis_idx];
    bool clamped = clamp_steps_to_limits(target, steps);
    if (steps == 0) {
      Serial.println(clamped ? "WARN LIMIT" : "OK MOVE");
      return;
    }

    step_axis(target, steps, delay_us);
    target.position_steps += steps;
    Serial.println(clamped ? "WARN LIMIT" : "OK MOVE");
    return;
  }

  if (line.startsWith("SET_STEPS_PER_MM")) {
    // Format: SET_STEPS_PER_MM X 80.0
    int first = line.indexOf(' ');
    int second = line.indexOf(' ', first + 1);
    if (first < 0 || second < 0) {
      Serial.println("ERR SET_STEPS_PER_MM");
      return;
    }

    String axis = line.substring(first + 1, second);
    int axis_idx = axis_index_from_name(axis);
    if (axis_idx < 0) {
      Serial.println("ERR AXIS");
      return;
    }

    float value = line.substring(second + 1).toFloat();
    if (value <= 0.0f) {
      Serial.println("ERR SET_STEPS_PER_MM");
      return;
    }

    axes[axis_idx].steps_per_mm = value;
    Serial.println("OK SET_STEPS_PER_MM");
    return;
  }

  if (line.startsWith("SET_LIMITS")) {
    // Format: SET_LIMITS X -10 200
    int first = line.indexOf(' ');
    int second = line.indexOf(' ', first + 1);
    int third = line.indexOf(' ', second + 1);
    if (first < 0 || second < 0 || third < 0) {
      Serial.println("ERR SET_LIMITS");
      return;
    }

    String axis = line.substring(first + 1, second);
    int axis_idx = axis_index_from_name(axis);
    if (axis_idx < 0) {
      Serial.println("ERR AXIS");
      return;
    }

    float min_mm = line.substring(second + 1, third).toFloat();
    float max_mm = line.substring(third + 1).toFloat();
    if (min_mm > max_mm) {
      Serial.println("ERR SET_LIMITS");
      return;
    }

    axes[axis_idx].min_mm = min_mm;
    axes[axis_idx].max_mm = max_mm;
    Serial.println("OK SET_LIMITS");
    return;
  }

  if (line.startsWith("ZERO")) {
    // Format: ZERO X
    int first = line.indexOf(' ');
    if (first < 0) {
      Serial.println("ERR ZERO");
      return;
    }

    String axis = line.substring(first + 1);
    int axis_idx = axis_index_from_name(axis);
    if (axis_idx < 0) {
      Serial.println("ERR AXIS");
      return;
    }

    axes[axis_idx].position_steps = 0;
    Serial.println("OK ZERO");
    return;
  }

  if (line.equalsIgnoreCase("STATUS")) {
    Serial.print("STATUS");
    for (int i = 0; i < axis_count; i++) {
      float mm = steps_to_mm(axes[i], axes[i].position_steps);
      Serial.print(" ");
      Serial.print(axes[i].name);
      Serial.print("=");
      Serial.print(mm, 3);
    }
    Serial.println();
    return;
  }

  Serial.println("ERR CMD");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    handle_command(line);
  }
}
