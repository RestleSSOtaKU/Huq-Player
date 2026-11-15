use std::f32::consts::PI;

#[derive(Clone, Copy, Default)]
pub struct Biquad {
    b0: f32,
    b1: f32,
    b2: f32,
    a1: f32,
    a2: f32,
    z1: f32,
    z2: f32,
}

impl Biquad {
    pub fn reset(&mut self) {
        self.z1 = 0.0;
        self.z2 = 0.0;
    }

    pub fn set_low_shelf(&mut self, fc: f32, gain_db: f32, q: f32, fs: f32) {
        let a = 10.0_f32.powf(gain_db / 40.0);
        let w0 = 2.0 * PI * fc / fs;
        let (sw, cw) = w0.sin_cos();
        let alpha = sw / (2.0 * q);
        let sq_a = a.sqrt();

        let b0 = a * ((a + 1.0) - (a - 1.0) * cw + 2.0 * sq_a * alpha);
        let b1 = 2.0 * a * ((a - 1.0) - (a + 1.0) * cw);
        let b2 = a * ((a + 1.0) - (a - 1.0) * cw - 2.0 * sq_a * alpha);
        let a0 = (a + 1.0) + (a - 1.0) * cw + 2.0 * sq_a * alpha;
        let a1 = -2.0 * ((a - 1.0) + (a + 1.0) * cw);
        let a2 = (a + 1.0) + (a - 1.0) * cw - 2.0 * sq_a * alpha;

        self.b0 = b0 / a0;
        self.b1 = b1 / a0;
        self.b2 = b2 / a0;
        self.a1 = a1 / a0;
        self.a2 = a2 / a0;
    }

    pub fn set_high_shelf(&mut self, fc: f32, gain_db: f32, q: f32, fs: f32) {
        let a = 10.0_f32.powf(gain_db / 40.0);
        let w0 = 2.0 * PI * fc / fs;
        let (sw, cw) = w0.sin_cos();
        let alpha = sw / (2.0 * q);
        let sq_a = a.sqrt();

        let b0 = a * ((a + 1.0) + (a - 1.0) * cw + 2.0 * sq_a * alpha);
        let b1 = -2.0 * a * ((a - 1.0) + (a + 1.0) * cw);
        let b2 = a * ((a + 1.0) + (a - 1.0) * cw - 2.0 * sq_a * alpha);
        let a0 = (a + 1.0) - (a - 1.0) * cw + 2.0 * sq_a * alpha;
        let a1 = 2.0 * ((a - 1.0) - (a + 1.0) * cw);
        let a2 = (a + 1.0) - (a - 1.0) * cw - 2.0 * sq_a * alpha;

        self.b0 = b0 / a0;
        self.b1 = b1 / a0;
        self.b2 = b2 / a0;
        self.a1 = a1 / a0;
        self.a2 = a2 / a0;
    }

    #[inline(always)]
    pub fn process_sample(&mut self, x: f32) -> f32 {
        let y = self.b0 * x + self.z1;
        self.z1 = self.b1 * x - self.a1 * y + self.z2;
        self.z2 = self.b2 * x - self.a2 * y;
        y
    }
}
