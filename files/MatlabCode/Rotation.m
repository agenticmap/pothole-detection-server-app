for i=1:length(GyroZmax)
v=[GyroZmax(i),GyroYmax(i),GyroXmax(i)];
RotMat = eul2rotm(v);
RotVec = [LinAccXmax(i),LinAccYmax(i),LinAccZmax(i)];
RotatedVec = RotVec * RotMat;
SumUp(i,:) = [v,RotatedVec];
end